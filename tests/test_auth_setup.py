"""Authentication wizard flow and YAML persistence without live requests."""

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
from gamdl.cli.tui.wizard import WizardBack


class _FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass


class _ScriptedWizard:
    """Drive the same real wizard coroutine without opening a terminal."""

    def __init__(self, method=0, inputs=(), failure_action=None, **_kwargs):
        self.method = method
        self.inputs = iter(inputs)
        self.failure_action = failure_action
        self.prompts = []
        self.screens = []

    async def run(self, flow):
        return await flow(self)

    async def choose(self, title, choices, *, description=""):
        self.screens.append((title, description))
        if title in {"Welcome to Gamdl!", "Configure authentication"}:
            return self.method
        if title == "Authentication failed":
            if self.failure_action is None:
                raise click.Abort()
            return self.failure_action
        return 0

    async def ask(self, title, label, *, description="", default="", password=False):
        self.prompts.append((title, label, default, password))
        value = next(self.inputs)
        if isinstance(value, Exception):
            raise value
        return value

    def status(self, title, description):
        self.screens.append((title, description))


class AuthSetupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.yaml"
        self.runner = CliRunner()

    def invoke(self, *args):
        return self.runner.invoke(main, ["--config", str(self.path), *args])

    def test_wrapper_url_prefilled_and_saved_only_after_validation(self):
        self.path.write_text("auth:\n  wrapper_url: http://custom-wrapper:8080\n")
        wizard = _ScriptedWizard(method=0, inputs=["http://new-wrapper:7777"])
        with (
            patch("gamdl.cli.session.require_terminal"),
            patch("gamdl.cli.session.FullScreenWizard", return_value=wizard),
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
        self.assertEqual(
            wizard.prompts[0],
            (
                "Wrapper authentication",
                "Wrapper URL",
                "http://custom-wrapper:8080",
                False,
            ),
        )
        self.assertEqual(
            connect.await_args.kwargs["base_url"], "http://new-wrapper:7777"
        )
        saved = yaml.safe_load(self.path.read_text())
        self.assertEqual(saved["auth"]["mode"], "wrapper")
        self.assertEqual(saved["auth"]["wrapper_url"], "http://new-wrapper:7777")
        self.assertTrue(
            any(title == "Authentication successful" for title, _ in wizard.screens)
        )

    def test_direct_token_saved_but_redacted_from_config_show(self):
        wizard = _ScriptedWizard(method=2, inputs=["secret-123"])
        with (
            patch("gamdl.cli.session.require_terminal"),
            patch("gamdl.cli.session.FullScreenWizard", return_value=wizard),
            patch(
                "gamdl.cli.session.AppleMusicApi.create",
                new_callable=AsyncMock,
                return_value=_FakeClient(),
            ) as create,
        ):
            result = self.invoke("auth")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(create.await_args.kwargs["media_user_token"], "secret-123")
        self.assertTrue(wizard.prompts[0][3])
        self.assertEqual(load_config(self.path).auth.media_user_token, "secret-123")
        self.assertEqual(load_config(self.path).auth.mode, "media-user-token")
        shown = self.invoke("config", "show")
        self.assertNotIn("secret-123", shown.output)
        self.assertIn("REDACTED", shown.output)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_failed_authentication_remains_fullscreen_and_does_not_save(self):
        self.path.write_text("auth:\n  wrapper_url: http://localhost\n")
        load_config(self.path)
        before = self.path.read_text()
        wizard = _ScriptedWizard(method=2, inputs=["bad-token"])
        with (
            patch("gamdl.cli.session.require_terminal"),
            patch("gamdl.cli.session.FullScreenWizard", return_value=wizard),
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
        self.assertTrue(
            any(title == "Authentication failed" for title, _ in wizard.screens)
        )

    def test_retry_authentication_without_reentering_credentials(self):
        wizard = _ScriptedWizard(method=2, inputs=["token-123"], failure_action=0)
        with (
            patch("gamdl.cli.session.require_terminal"),
            patch("gamdl.cli.session.FullScreenWizard", return_value=wizard),
            patch(
                "gamdl.cli.session.AppleMusicApi.create",
                new_callable=AsyncMock,
                side_effect=[GamdlAuthenticationError("Rejected"), _FakeClient()],
            ) as create,
        ):
            result = self.invoke("auth")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(create.await_count, 2)
        self.assertEqual(len(wizard.prompts), 1)
        self.assertEqual(load_config(self.path).auth.media_user_token, "token-123")
        self.assertTrue(
            any(title == "Authentication failed" for title, _ in wizard.screens)
        )

    def test_modes_and_noninteractive_auth_command(self):
        self.assertIsNone(load_config(None).auth.mode)
        self.path.write_text("auth:\n  mode: cookies\n")
        self.assertEqual(load_config(self.path).auth.mode, "cookies")
        result = self.runner.invoke(main, ["--no-config", "auth"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("requires YAML configuration", result.output)

    def test_profile_saves_only_to_active_profile(self):
        self.path.write_text(
            "auth:\n  mode: wrapper\nprofiles:\n  alternate:\n    auth:\n      mode: null\n"
        )
        wizard = _ScriptedWizard(method=2, inputs=["profile-token"])
        with (
            patch("gamdl.cli.session.require_terminal"),
            patch("gamdl.cli.session.FullScreenWizard", return_value=wizard),
            patch(
                "gamdl.cli.session.AppleMusicApi.create",
                new_callable=AsyncMock,
                return_value=_FakeClient(),
            ),
        ):
            result = self.runner.invoke(
                main, ["--config", str(self.path), "--profile", "alternate", "auth"]
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(load_config(self.path).auth.mode, "wrapper")
        self.assertEqual(
            load_config(self.path, profile="alternate").auth.mode, "media-user-token"
        )
        self.assertIsNone(load_config(self.path).auth.media_user_token)


class FirstRunAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_cookies_setup_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            cookies_path = Path(directory) / "cookies.txt"
            cookies_path.touch()
            settings = load_config(None).auth
            options = SimpleNamespace(**{**vars(settings), "auth_mode": settings.mode})
            wizard = _ScriptedWizard(method=1, inputs=[str(cookies_path)])
            root = click.Context(
                click.Command("test"),
                obj={"config_path": config_path, "profile": None, "no_config": False},
            )
            with (
                root,
                patch("gamdl.cli.session.require_terminal"),
                patch("gamdl.cli.session.FullScreenWizard", return_value=wizard),
                patch(
                    "gamdl.cli.session.AppleMusicApi.from_cookies",
                    new_callable=AsyncMock,
                    return_value=_FakeClient(),
                ) as authenticate_cookies,
            ):
                async with AsyncExitStack() as stack:
                    await authenticate(options, stack, console=Console(quiet=True))
            self.assertEqual(wizard.prompts[0][2], settings.cookies_path)
            self.assertEqual(authenticate_cookies.await_args.args[0], str(cookies_path))
            saved = load_config(config_path)
            self.assertEqual(saved.auth.mode, "cookies")
            self.assertEqual(saved.auth.cookies_path, str(cookies_path))

    async def test_back_from_input_returns_to_method_selector(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            options = SimpleNamespace(
                **{**vars(load_config(None).auth), "auth_mode": None}
            )

            class _BackWizard(_ScriptedWizard):
                def __init__(self):
                    super().__init__(method=0, inputs=[WizardBack(), "new-token"])
                    self.selections = 0

                async def choose(self, title, choices, *, description=""):
                    if title == "Welcome to Gamdl!":
                        self.selections += 1
                        return 0 if self.selections == 1 else 2
                    return await super().choose(title, choices, description=description)

            wizard = _BackWizard()
            root = click.Context(
                click.Command("test"),
                obj={"config_path": path, "profile": None, "no_config": False},
            )
            with (
                root,
                patch("gamdl.cli.session.require_terminal"),
                patch("gamdl.cli.session.FullScreenWizard", return_value=wizard),
                patch(
                    "gamdl.cli.session.AppleMusicApi.create",
                    new_callable=AsyncMock,
                    return_value=_FakeClient(),
                ),
            ):
                async with AsyncExitStack() as stack:
                    await authenticate(options, stack, console=Console(quiet=True))
            self.assertEqual(wizard.selections, 2)
            self.assertEqual(load_config(path).auth.mode, "media-user-token")
