"""Authentication wizard and persistence without real Apple Music requests."""

import tempfile
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import click
import yaml
from click.testing import CliRunner
from rich.console import Console

from gamdl.api import GamdlAuthenticationError
from gamdl.cli.config.loader import load_config
from gamdl.cli.main import main
from gamdl.cli.session import authenticate


class _FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class AuthSetupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.yaml"
        self.runner = CliRunner()

    def invoke(self, *args):
        return self.runner.invoke(main, ["--config", str(self.path), *args])

    def test_wrapper_url_uses_yaml_default_and_saves_validated_choice(self):
        self.path.write_text("auth:\n  wrapper_url: http://custom-wrapper:8080\n")
        with (
            patch("gamdl.cli.session.require_terminal"),
            patch(
                "gamdl.cli.session.select_one", new_callable=AsyncMock, return_value=0
            ),
            patch(
                "gamdl.cli.session.ask_text",
                new_callable=AsyncMock,
                return_value="http://new-wrapper:7777",
            ) as ask,
            patch(
                "gamdl.cli.session.WrapperApi.create",
                new_callable=AsyncMock,
                return_value=_FakeClient(),
            ) as connect,
            patch(
                "gamdl.cli.session.AppleMusicApi.from_wrapper",
                new_callable=AsyncMock,
                return_value=_FakeClient(),
            ),
        ):
            result = self.invoke("auth")
        self.assertEqual(result.exit_code, 0, result.output)
        ask.assert_awaited_once_with(
            "Wrapper URL:", default="http://custom-wrapper:8080"
        )
        self.assertEqual(
            connect.await_args.kwargs["base_url"], "http://new-wrapper:7777"
        )
        saved = yaml.safe_load(self.path.read_text())
        self.assertEqual(saved["auth"]["mode"], "wrapper")
        self.assertEqual(saved["auth"]["wrapper_url"], "http://new-wrapper:7777")

    def test_direct_token_is_saved_but_redacted_from_show(self):
        with (
            patch("gamdl.cli.session.require_terminal"),
            patch(
                "gamdl.cli.session.select_one", new_callable=AsyncMock, return_value=2
            ),
            patch(
                "gamdl.cli.session.ask_text",
                new_callable=AsyncMock,
                return_value="secret-123",
            ) as ask,
            patch(
                "gamdl.cli.session.AppleMusicApi.create",
                new_callable=AsyncMock,
                return_value=_FakeClient(),
            ) as create,
        ):
            result = self.invoke("auth")
        self.assertEqual(result.exit_code, 0, result.output)
        ask.assert_awaited_once_with("Media User Token:", password=True)
        self.assertEqual(create.await_args.kwargs["media_user_token"], "secret-123")
        self.assertEqual(load_config(self.path).auth.media_user_token, "secret-123")
        self.assertEqual(load_config(self.path).auth.mode, "media-user-token")
        shown = self.invoke("config", "show")
        self.assertEqual(shown.exit_code, 0, shown.output)
        self.assertNotIn("secret-123", shown.output)
        self.assertIn("REDACTED", shown.output)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_invalid_credentials_are_not_saved(self):
        self.path.write_text("auth:\n  wrapper_url: http://localhost\n")
        load_config(self.path)  # Perform normal schema upgrade before comparison.
        before = self.path.read_text()
        with (
            patch("gamdl.cli.session.require_terminal"),
            patch(
                "gamdl.cli.session.select_one", new_callable=AsyncMock, return_value=2
            ),
            patch(
                "gamdl.cli.session.ask_text",
                new_callable=AsyncMock,
                return_value="bad-token",
            ),
            patch(
                "gamdl.cli.session.AppleMusicApi.create",
                new_callable=AsyncMock,
                side_effect=GamdlAuthenticationError("Rejected"),
            ),
        ):
            result = self.invoke("auth")
        self.assertEqual(result.exit_code, 1)
        self.assertNotIn("bad-token", result.output)
        self.assertEqual(self.path.read_text(), before)

    def test_default_mode_is_null_and_old_explicit_modes_are_kept(self):
        self.assertIsNone(load_config(None).auth.mode)
        self.path.write_text("auth:\n  mode: cookies\n")
        self.assertEqual(load_config(self.path).auth.mode, "cookies")

    def test_auth_command_requires_config(self):
        result = self.runner.invoke(main, ["--no-config", "auth"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("requires YAML configuration", result.output)

    def test_selected_method_is_saved_in_active_profile_only(self):
        self.path.write_text(
            "auth:\n  mode: wrapper\nprofiles:\n  alternate:\n    auth:\n      mode: null\n"
        )
        with (
            patch("gamdl.cli.session.require_terminal"),
            patch(
                "gamdl.cli.session.select_one", new_callable=AsyncMock, return_value=2
            ),
            patch(
                "gamdl.cli.session.ask_text",
                new_callable=AsyncMock,
                return_value="profile-token",
            ),
            patch(
                "gamdl.cli.session.AppleMusicApi.create",
                new_callable=AsyncMock,
                return_value=_FakeClient(),
            ),
        ):
            result = self.runner.invoke(
                main,
                ["--config", str(self.path), "--profile", "alternate", "auth"],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(load_config(self.path).auth.mode, "wrapper")
        self.assertEqual(
            load_config(self.path, profile="alternate").auth.mode, "media-user-token"
        )
        self.assertIsNone(load_config(self.path).auth.media_user_token)


class FirstRunAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_cookies_auth_prompts_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            cookies_path = Path(directory) / "cookies.txt"
            cookies_path.touch()
            settings = load_config(None).auth
            options = SimpleNamespace(**{**vars(settings), "auth_mode": settings.mode})
            root = click.Context(
                click.Command("test"),
                obj={
                    "config_path": config_path,
                    "profile": None,
                    "no_config": False,
                },
            )
            with (
                root,
                patch("gamdl.cli.session.require_terminal"),
                patch(
                    "gamdl.cli.session.select_one",
                    new_callable=AsyncMock,
                    return_value=1,
                ) as select,
                patch(
                    "gamdl.cli.session.ask_text",
                    new_callable=AsyncMock,
                    return_value=str(cookies_path),
                ) as ask,
                patch(
                    "gamdl.cli.session.AppleMusicApi.from_cookies",
                    new_callable=AsyncMock,
                    return_value=_FakeClient(),
                ) as authenticate_cookies,
            ):
                async with AsyncExitStack() as stack:
                    await authenticate(
                        options, stack, console=Console(force_terminal=False)
                    )
            self.assertEqual(select.await_count, 1)
            ask.assert_awaited_once_with("Cookies file:", default=settings.cookies_path)
            self.assertEqual(authenticate_cookies.await_args.args[0], str(cookies_path))
            saved = load_config(config_path)
            self.assertEqual(saved.auth.mode, "cookies")
            self.assertEqual(saved.auth.cookies_path, str(cookies_path))
