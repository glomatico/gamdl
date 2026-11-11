"""Output-directory setup uses the OS Music path and the existing full-screen UI."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import click
from click.testing import CliRunner
from prompt_toolkit.input import create_pipe_input

from gamdl.cli.commands.runner import run_rip
from gamdl.cli.config.loader import load_config
from gamdl.cli.main import main
from gamdl.cli.options import RipOptions
from gamdl.cli.output_setup import resolve_output_directory
from gamdl.cli.tui.wizard import FullScreenWizard
from tests.test_tui_menus import AlternateScreenOutput


class OutputSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_rip_resolves_missing_directory_after_authentication(self):
        options = RipOptions(
            urls=("https://music.apple.com/br/song/example/1",),
            no_tracebacks=True,
        )
        with (
            patch(
                "gamdl.cli.commands.runner._authenticate",
                new_callable=AsyncMock,
                return_value=(SimpleNamespace(active_subscription=True), None),
            ),
            patch(
                "gamdl.cli.commands.runner.resolve_output_directory",
                new_callable=AsyncMock,
                return_value="/tmp/gamdl-test-music",
            ) as select,
            patch(
                "gamdl.cli.commands.runner.create_drm",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Stop after selecting output directory"),
            ),
        ):
            self.assertEqual(await run_rip(options), 1)
            select.assert_awaited_once_with(None)
        self.assertEqual(options.output_dir, "/tmp/gamdl-test-music")

    async def test_os_music_default_is_confirmed_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp, create_pipe_input() as pipe:
            base = Path(tmp)
            music = base / "Windows Music"
            config = base / "config.yml"
            config.write_text("auth:\n  mode: wrapper\ndownload:\n  overwrite: true\n")
            output = AlternateScreenOutput()
            wizard = FullScreenWizard(input=pipe, output=output)
            root = click.Context(
                click.Command("test"),
                obj={"config_path": config, "no_config": False},
            )
            with (
                root,
                patch("gamdl.cli.output_setup.interactive", return_value=True),
                patch("gamdl.cli.output_setup.user_music_path", return_value=music),
                patch("gamdl.cli.output_setup.FullScreenWizard", return_value=wizard),
            ):
                task = asyncio.create_task(resolve_output_directory(None))
                await asyncio.wait_for(output.started.wait(), 2)
                self.assertEqual(wizard.buffer.text, str(music))
                pipe.send_text("\r")
                chosen = await asyncio.wait_for(task, 2)
            self.assertEqual(chosen, str(music))
            self.assertTrue(music.is_dir())
            self.assertEqual(load_config(config).output.directory, str(music))
            self.assertEqual(load_config(config).auth.mode, "wrapper")
            self.assertTrue(load_config(config).download.overwrite)
            self.assertEqual((output.entered, output.left), (1, 1))

    async def test_custom_path_is_saved_and_preconfigured_path_bypasses_prompt(self):
        with tempfile.TemporaryDirectory() as tmp, create_pipe_input() as pipe:
            base = Path(tmp)
            music = base / "Suggested Music"
            custom = base / "Custom Music"
            config = base / "config.yml"
            output = AlternateScreenOutput()
            wizard = FullScreenWizard(input=pipe, output=output)
            root = click.Context(
                click.Command("test"),
                obj={"config_path": config, "no_config": False},
            )
            with (
                root,
                patch("gamdl.cli.output_setup.interactive", return_value=True),
                patch("gamdl.cli.output_setup.user_music_path", return_value=music),
                patch("gamdl.cli.output_setup.FullScreenWizard", return_value=wizard),
            ):
                task = asyncio.create_task(resolve_output_directory(None))
                await asyncio.wait_for(output.started.wait(), 2)
                pipe.send_text("\x15" + str(custom) + "\r")
                self.assertEqual(await asyncio.wait_for(task, 2), str(custom))
            self.assertTrue(custom.is_dir())
            self.assertEqual(load_config(config).output.directory, str(custom))
            self.assertFalse(music.exists())

        with patch("gamdl.cli.output_setup.interactive") as is_interactive:
            self.assertEqual(await resolve_output_directory(str(custom)), str(custom))
            is_interactive.assert_not_called()

    async def test_no_config_does_not_persist_selection(self):
        with tempfile.TemporaryDirectory() as tmp, create_pipe_input() as pipe:
            base = Path(tmp)
            music = base / "Music"
            config = base / "config.yml"
            output = AlternateScreenOutput()
            wizard = FullScreenWizard(input=pipe, output=output)
            root = click.Context(
                click.Command("test"),
                obj={"config_path": config, "no_config": True},
            )
            with (
                root,
                patch("gamdl.cli.output_setup.interactive", return_value=True),
                patch("gamdl.cli.output_setup.user_music_path", return_value=music),
                patch("gamdl.cli.output_setup.FullScreenWizard", return_value=wizard),
            ):
                task = asyncio.create_task(resolve_output_directory(None))
                await asyncio.wait_for(output.started.wait(), 2)
                pipe.send_text("\r")
                self.assertEqual(await asyncio.wait_for(task, 2), str(music))
            self.assertTrue(music.is_dir())
            self.assertFalse(config.exists())

    async def test_noninteractive_requires_explicit_path(self):
        with (
            patch("gamdl.cli.output_setup.interactive", return_value=False),
            self.assertRaisesRegex(click.ClickException, "--output-dir PATH"),
        ):
            await resolve_output_directory(None)

    def test_unconfigured_yaml_and_maintenance_report_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yml"
            result = CliRunner().invoke(
                main, ["--config", str(config), "config", "init"]
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIsNone(load_config(config).output.directory)
            info = CliRunner().invoke(
                main, ["--config", str(config), "database", "info"]
            )
            self.assertEqual(info.exit_code, 0, info.output)
            self.assertIn("Database:", info.output)
            prune = CliRunner().invoke(
                main, ["--config", str(config), "database", "prune", "--dry-run"]
            )
            self.assertNotEqual(prune.exit_code, 0)
            self.assertIn("Output directory is not configured", prune.output)
