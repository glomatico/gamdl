"""Offline tests for live-suite authentication ordering; no Apple requests."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from gamdl.api import GamdlTransportError
from tests.live_auth import LiveSettings, open_live_session


class FakeApi:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass


class AuthPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrapper_wins_over_token_and_cookies(self):
        wrapper = type(
            "FakeWrapper", (), {"me": {"auth": {"state": "authenticated"}}}
        )()
        api = FakeApi()
        settings = LiveSettings(
            media_user_token="secret-token",
            cookies_path="ignored",
            developer_token="dev",
        )
        with (
            patch(
                "tests.live_auth.WrapperApi.create", new=AsyncMock(return_value=wrapper)
            ) as w,
            patch(
                "tests.live_auth.AppleMusicApi.from_wrapper",
                new=AsyncMock(return_value=api),
            ) as from_wrapper,
            patch(
                "tests.live_auth.AppleMusicApi.create", new=AsyncMock()
            ) as from_token,
            patch(
                "tests.live_auth.AppleMusicApi.from_cookies", new=AsyncMock()
            ) as from_cookies,
        ):
            async with open_live_session(settings) as session:
                self.assertEqual(session.source, "wrapper")
                self.assertIs(session.api, api)
                self.assertIs(session.wrapper, wrapper)
                self.assertNotIn("secret-token", repr(session))
            w.assert_awaited_once()
            from_wrapper.assert_awaited_once()
            from_token.assert_not_awaited()
            from_cookies.assert_not_awaited()

    async def test_media_user_token_is_second_choice(self):
        settings = LiveSettings(
            media_user_token="secret-token",
            developer_token="dev",
            cookies_path="ignored",
        )
        with (
            patch(
                "tests.live_auth.WrapperApi.create",
                new=AsyncMock(side_effect=GamdlTransportError("offline")),
            ),
            patch(
                "tests.live_auth.AppleMusicApi.create",
                new=AsyncMock(return_value=FakeApi()),
            ) as token,
            patch(
                "tests.live_auth.AppleMusicApi.from_cookies", new=AsyncMock()
            ) as cookies,
        ):
            async with open_live_session(settings) as session:
                self.assertEqual(session.source, "media_user_token")
                self.assertIsNone(session.wrapper)
            token.assert_awaited_once()
            cookies.assert_not_awaited()

    async def test_cookie_file_is_last_choice(self):
        settings = LiveSettings(
            media_user_token="secret-token",
            developer_token="dev",
            cookies_path="cookies.txt",
        )
        with (
            patch(
                "tests.live_auth.WrapperApi.create",
                new=AsyncMock(side_effect=GamdlTransportError("offline")),
            ),
            patch(
                "tests.live_auth.AppleMusicApi.create",
                new=AsyncMock(side_effect=GamdlTransportError("invalid-token")),
            ),
            patch(
                "tests.live_auth.AppleMusicApi.from_cookies",
                new=AsyncMock(return_value=FakeApi()),
            ) as cookies,
        ):
            async with open_live_session(settings) as session:
                self.assertEqual(session.source, "cookies")
                self.assertIsNone(session.wrapper)
            cookies.assert_awaited_once()

    async def test_missing_all_sources_fails_without_secrets(self):
        with patch(
            "tests.live_auth.WrapperApi.create",
            new=AsyncMock(side_effect=GamdlTransportError("private-data")),
        ):
            with self.assertRaises(RuntimeError) as failure:
                async with open_live_session(LiveSettings()):
                    pass
            self.assertNotIn("private-data", str(failure.exception))

    async def test_logged_out_wrapper_is_not_selected(self):
        wrapper = type("FakeWrapper", (), {"me": {"auth": {"state": "logged_out"}}})()
        settings = LiveSettings(
            media_user_token="test-user-token", developer_token="test-dev"
        )
        with (
            patch(
                "tests.live_auth.WrapperApi.create",
                new=AsyncMock(return_value=wrapper),
            ),
            patch(
                "tests.live_auth.AppleMusicApi.from_wrapper", new=AsyncMock()
            ) as wrapped,
            patch(
                "tests.live_auth.AppleMusicApi.create",
                new=AsyncMock(return_value=FakeApi()),
            ),
        ):
            async with open_live_session(settings) as session:
                self.assertEqual(session.source, "media_user_token")
            wrapped.assert_not_awaited()


class EnvConfigurationTests(unittest.TestCase):
    def test_explicit_environment_variables_override_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / ".env").write_text(
                "GAMDL_WRAPPER_URL=http://from-file\n"
                "GAMDL_APPLE_MEDIA_USER_TOKEN=fake-secret-from-file\n"
                "GAMDL_APPLE_STOREFRONT=us\n",
                encoding="utf8",
            )
            with (
                patch("tests.live_auth.ROOT", Path(directory)),
                patch.dict(
                    os.environ,
                    {"GAMDL_WRAPPER_URL": "http://from-process"},
                    clear=True,
                ),
            ):
                settings = LiveSettings.from_env()

            self.assertEqual(settings.wrapper_url, "http://from-process")
            self.assertEqual(settings.storefront, "us")
            self.assertIsNotNone(settings.media_user_token)
            self.assertNotIn("fake-secret-from-file", repr(settings))


if __name__ == "__main__":
    unittest.main()
