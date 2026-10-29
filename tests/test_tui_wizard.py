"""Keyboard-driven tests for the persistent full-screen wizard."""

import asyncio
import tempfile
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import click
from prompt_toolkit.input import create_pipe_input
from rich.console import Console

from gamdl.cli.config.loader import load_config
from gamdl.cli.session import authenticate
from gamdl.cli.tui.components import MenuChoice
from gamdl.cli.tui.wizard import FullScreenWizard, WizardBack
from tests.test_tui_menus import AlternateScreenOutput


class WizardTuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrapper_credentials_and_two_factor_stay_in_same_screen(self):
        class FakeApi:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

        captured = {}

        async def wrapper_create(**kwargs):
            captured["url"] = kwargs["base_url"]
            captured["credentials"] = await kwargs["get_credentials"]()
            captured["code"] = await kwargs["get_2fa_code"]()
            return FakeApi()

        with tempfile.TemporaryDirectory() as folder, create_pipe_input() as pipe:
            path = Path(folder) / "config.yaml"
            options = SimpleNamespace(
                **{**vars(load_config(None).auth), "auth_mode": None}
            )
            output = AlternateScreenOutput()
            wizard = FullScreenWizard(input=pipe, output=output)
            root = click.Context(
                click.Command("test"),
                obj={"config_path": path, "profile": None, "no_config": False},
            )
            with (
                root,
                patch("gamdl.cli.session.require_terminal"),
                patch("gamdl.cli.session.FullScreenWizard", return_value=wizard),
                patch(
                    "gamdl.cli.session.WrapperApi.create", side_effect=wrapper_create
                ),
                patch(
                    "gamdl.cli.session.AppleMusicApi.from_wrapper",
                    new_callable=AsyncMock,
                    return_value=FakeApi(),
                ),
            ):
                async with AsyncExitStack() as stack:
                    task = asyncio.create_task(
                        authenticate(options, stack, console=Console(quiet=True))
                    )
                    await asyncio.wait_for(output.started.wait(), 2)
                    for typed in (
                        "\r",  # Wrapper selected
                        "\r",  # Keep prefilled URL
                        "person@example.com\r",
                        "password123\r",
                        "123456\r",
                        "\r",  # Finish
                    ):
                        pipe.send_text(typed)
                        await asyncio.sleep(0.09)
                    api, wrapper = await asyncio.wait_for(task, 2)
                    self.assertIsInstance(api, FakeApi)
                    self.assertIsInstance(wrapper, FakeApi)
            self.assertEqual(captured["url"], "http://127.0.0.1")
            self.assertEqual(
                captured["credentials"], ("person@example.com", "password123")
            )
            self.assertEqual(captured["code"], "123456")
            self.assertEqual(load_config(path).auth.mode, "wrapper")
            self.assertEqual((output.entered, output.left), (1, 1))

    async def test_choice_entry_status_and_success_use_one_alternate_screen(self):
        output = AlternateScreenOutput()
        with create_pipe_input() as pipe:
            wizard = FullScreenWizard(input=pipe, output=output)

            async def flow(ui):
                method = await ui.choose(
                    "Welcome to Gamdl!",
                    [MenuChoice("Wrapper", 0), MenuChoice("Media User Token", 1)],
                )
                address = await ui.ask(
                    "Wrapper authentication", "Wrapper URL", default="http://localhost"
                )
                ui.status("Authenticating", "Connecting...")
                await asyncio.sleep(0.03)
                await ui.choose(
                    "Authentication successful", [MenuChoice("Continue", 0)]
                )
                return method, address

            task = asyncio.create_task(wizard.run(flow))
            await asyncio.wait_for(output.started.wait(), 2)
            self.assertEqual(wizard.title, "Welcome to Gamdl!")
            pipe.send_text("\x1b[B\r")
            await asyncio.sleep(0.06)
            self.assertEqual(wizard.title, "Wrapper authentication")
            self.assertEqual(wizard.buffer.text, "http://localhost")
            pipe.send_text("\x15https://wrapper.test\r")  # Ctrl+U clears default
            await asyncio.sleep(0.07)
            self.assertIn(wizard.title, {"Authenticating", "Authentication successful"})
            pipe.send_text("\r")
            self.assertEqual(
                await asyncio.wait_for(task, 2), (1, "https://wrapper.test")
            )
        self.assertEqual((output.entered, output.left), (1, 1))

    async def test_backspace_edits_text_and_escape_returns_to_choices(self):
        output = AlternateScreenOutput()
        with create_pipe_input() as pipe:
            wizard = FullScreenWizard(input=pipe, output=output)

            async def flow(ui):
                attempts = 0
                while True:
                    await ui.choose("Choose method", [MenuChoice("Wrapper", 0)])
                    attempts += 1
                    try:
                        value = await ui.ask("Wrapper", "URL", default="old")
                        return attempts, value
                    except WizardBack:
                        continue

            task = asyncio.create_task(wizard.run(flow))
            await asyncio.wait_for(output.started.wait(), 2)
            pipe.send_text("\r")
            await asyncio.sleep(0.05)
            pipe.send_text("\x7f")
            await asyncio.sleep(0.05)
            self.assertEqual(wizard.buffer.text, "ol")
            pipe.send_text("\x1b")
            await asyncio.sleep(0.45)
            self.assertEqual(wizard.title, "Choose method")
            pipe.send_text("\r")
            await asyncio.sleep(0.05)
            pipe.send_text("\x15new\r")
            self.assertEqual(await asyncio.wait_for(task, 2), (2, "new"))
        self.assertEqual((output.entered, output.left), (1, 1))

    async def test_masked_token_and_control_c_restore_terminal(self):
        output = AlternateScreenOutput()
        with create_pipe_input() as pipe:
            wizard = FullScreenWizard(input=pipe, output=output)

            async def flow(ui):
                await ui.ask("Media User Token", "Token", password=True)
                return "unexpected"

            task = asyncio.create_task(wizard.run(flow))
            await asyncio.wait_for(output.started.wait(), 2)
            self.assertTrue(wizard.secret)
            pipe.send_text("do-not-print-this")
            await asyncio.sleep(0.05)
            pipe.send_text("\x03")
            with self.assertRaises(click.Abort):
                await asyncio.wait_for(task, 2)
        self.assertEqual((output.entered, output.left), (1, 1))
