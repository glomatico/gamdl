"""Startup warnings are informative, do not disclose account details or block rip."""

from __future__ import annotations

import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from rich.console import Console

from gamdl.cli.commands.rip import RipOptions
from gamdl.cli.commands.runner import run_rip
from gamdl.cli.warnings import (
    warn_alac_without_wrapper,
    warn_content_restrictions,
)
from gamdl.download import DownloadResult, MediaStatus
from gamdl.drm import Drm
from gamdl.media import (
    Song,
    SongCodec,
    StreamOrigin,
    StreamSelectionOption,
    StreamSelectionRequest,
)


class CliWarningsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.lines = StringIO()
        self.console = Console(file=self.lines, force_terminal=False)

    def test_account_content_restrictions(self):
        for restrictions, warned in (
            (None, False),
            ({}, False),
            ({"music": "explicit"}, True),
            ({"music": "clean", "musicVideos": "clean"}, True),
            ({"unexpected": True}, True),
            ("unrecognized", False),
        ):
            with self.subTest(restrictions=restrictions):
                self.lines.seek(0)
                self.lines.truncate()
                api = SimpleNamespace(account_restrictions=restrictions)
                warn_content_restrictions(api, self.console)
                self.assertEqual(
                    "Your Apple Music account has content restrictions enabled"
                    in self.lines.getvalue(),
                    warned,
                )
                self.assertNotIn("explicit", self.lines.getvalue())

    def test_alac_warning_respects_priority_exact_codec_and_wrapper(self):
        cases = (
            ("alac", (), None, False, True),
            ("alac-hires", (), None, False, True),
            ("alac-lossless", (), None, False, True),
            (None, ("alac", "aac"), None, False, True),
            (None, ("alac-hires", "alac-lossless"), None, False, True),
            (None, ("aac-web", "alac-lossless"), None, False, True),
            (None, ("aac-web",), None, False, False),
            ("aac-web", ("alac-hires",), None, False, False),
            (None, (), None, False, False),
            ("alac-hires", (), object(), False, False),
            ("alac", (), object(), False, False),
            (None, ("alac-lossless",), object(), False, False),
            (None, ("alac-lossless",), None, True, False),
        )
        for codec, priority, wrapper, ask, warned in cases:
            with self.subTest(codec=codec, priority=priority, wrapper=wrapper, ask=ask):
                self.lines.seek(0)
                self.lines.truncate()
                warn_alac_without_wrapper(
                    wrapper,
                    self.console,
                    codec=codec,
                    codec_priority=priority,
                    ask_song_codec=ask,
                )
                message = " ".join(self.lines.getvalue().split())
                self.assertEqual("ALAC selected without Wrapper" in message, warned)
                if warned:
                    self.assertIn("Widevine/PlayReady", message)
                else:
                    self.assertEqual(message, "")

    async def test_high_resolution_warning_reports_only_insufficient_drm(self):
        cases = (
            ("widevine", 3, 2160, True, "Widevine L3"),
            ("widevine", 2, 1440, True, "Widevine L2"),
            ("widevine", 1, 2160, False, None),
            ("widevine", 3, 1080, False, None),
            ("widevine", 3, 720, False, None),
            ("widevine", None, 2160, False, None),
            ("playready", 2000, 2160, True, "PlayReady SL2000"),
            ("playready", 150, 1440, True, "PlayReady SL150"),
            ("playready", 3000, 2160, False, None),
            ("playready", None, 2160, False, None),
        )
        from gamdl.cli.warnings import warn_high_resolution_security

        for backend, level, max_height, warned, description in cases:
            with self.subTest(backend=backend, level=level, height=max_height):
                self.lines.seek(0)
                self.lines.truncate()
                cdm = Mock(security_level=level)
                drm = (
                    Drm.from_widevine(Mock(), cdm=cdm)
                    if backend == "widevine"
                    else Drm.from_playready(Mock(), cdm=cdm)
                )
                await warn_high_resolution_security(
                    drm, self.console, max_height=max_height
                )
                message = " ".join(self.lines.getvalue().split())
                self.assertEqual("above 1080p" in message, warned)
                if warned:
                    self.assertIn(description, message)
                    self.assertIn(f"{max_height}p", message)
                    self.assertIn("Widevine L1 or PlayReady SL3000", message)
                else:
                    self.assertEqual(message, "")

    async def test_high_resolution_warning_ignores_unknown_test_drm(self):
        from gamdl.cli.warnings import warn_high_resolution_security

        await warn_high_resolution_security(Mock(), self.console, max_height=2160)
        self.assertEqual(self.lines.getvalue(), "")

    async def test_rip_warns_when_high_height_and_low_drm(self):
        from gamdl.cli.warnings import warn_high_resolution_security

        options = RipOptions(
            urls=("https://music.apple.com/br/song/example/1",),
            output_dir="Test Music",
            max_height=2160,
            no_tracebacks=True,
        )
        api = SimpleNamespace(active_subscription=True, account_restrictions=None)
        drm = Drm.from_widevine(Mock(), cdm=Mock(security_level=3))
        original_warning = warn_high_resolution_security

        async def record_warning(device, output, *, max_height):
            self.assertIs(device, drm)
            await original_warning(device, output, max_height=max_height)

        with (
            patch(
                "gamdl.cli.commands.runner.output_console", return_value=self.console
            ),
            patch(
                "gamdl.cli.commands.runner._authenticate",
                new_callable=AsyncMock,
                return_value=(api, None),
            ),
            patch(
                "gamdl.cli.commands.runner.create_drm",
                new_callable=AsyncMock,
                return_value=drm,
            ),
            patch(
                "gamdl.cli.commands.runner.warn_high_resolution_security",
                side_effect=record_warning,
            ),
            patch(
                "gamdl.cli.commands.runner.Downloader",
                side_effect=RuntimeError("stop after warning"),
            ),
        ):
            self.assertEqual(await run_rip(options), 1)
        self.assertIn("Widevine L3", self.lines.getvalue())

    async def test_rip_prints_both_warnings_after_authentication(self):
        options = RipOptions(
            urls=("https://music.apple.com/br/song/example/1",),
            output_dir="Test Music",
            song_codec_priority=("alac-hires", "alac-lossless"),
            no_tracebacks=True,
        )
        api = SimpleNamespace(
            active_subscription=True,
            account_restrictions={"music": "explicit"},
        )
        with (
            patch(
                "gamdl.cli.commands.runner.output_console", return_value=self.console
            ),
            patch(
                "gamdl.cli.commands.runner._authenticate",
                new_callable=AsyncMock,
                return_value=(api, None),
            ),
            patch(
                "gamdl.cli.commands.runner.create_drm",
                new_callable=AsyncMock,
                side_effect=RuntimeError("stop after startup checks"),
            ),
        ):
            self.assertEqual(await run_rip(options), 1)
        message = self.lines.getvalue()
        self.assertLess(
            message.index("Authenticated"),
            message.index("Your Apple Music account has content restrictions"),
        )
        self.assertIn("ALAC selected without Wrapper", message)
        self.assertNotIn("explicit", message)

    async def test_wrapper_with_no_restrictions_prints_neither_warning(self):
        options = RipOptions(
            urls=("https://music.apple.com/br/song/example/1",),
            output_dir="Test Music",
            song_codec="alac-hires",
            no_tracebacks=True,
        )
        api = SimpleNamespace(
            active_subscription=True,
            account_restrictions=None,
        )
        with (
            patch(
                "gamdl.cli.commands.runner.output_console", return_value=self.console
            ),
            patch(
                "gamdl.cli.commands.runner._authenticate",
                new_callable=AsyncMock,
                return_value=(api, Mock()),
            ),
            patch(
                "gamdl.cli.commands.runner.create_drm",
                new_callable=AsyncMock,
                side_effect=RuntimeError("stop after startup checks"),
            ),
        ):
            self.assertEqual(await run_rip(options), 1)
        message = self.lines.getvalue()
        self.assertNotIn("content restrictions", message)
        self.assertNotIn("ALAC selected without Wrapper", message)

    async def test_interactive_alac_selection_warns_once_for_the_session(self):
        api = SimpleNamespace(
            active_subscription=True, account_restrictions=None, storefront="br"
        )
        songs = [Song.from_id(str(i), api=api) for i in (1001, 1002)]
        for song in songs:
            song.get_metadata = AsyncMock(
                return_value={"attributes": {"name": "Test Song"}}
            )

        class Resolver:
            def __init__(self, _api, **_kwargs):
                pass

            async def from_url(self, _url):
                for song in songs:
                    yield song

        stream_request = StreamSelectionRequest(
            "1001",
            StreamOrigin.ASSET_ENHANCED_HLS,
            (
                StreamSelectionOption(0, Mock(codec=SongCodec.AAC)),
                StreamSelectionOption(1, Mock(codec=SongCodec.ALAC_HIRES)),
            ),
        )

        async def download(media, **kwargs):
            selected = await kwargs["selector"](stream_request)
            self.assertEqual(selected, 1)
            return DownloadResult(
                media.id,
                Path("test.m4a"),
                MediaStatus.DOWNLOADED,
                32,
                SongCodec.ALAC_HIRES,
            )

        downloader = Mock(itunes_api=Mock())
        downloader.download = AsyncMock(side_effect=download)
        with (
            patch(
                "gamdl.cli.commands.runner.output_console", return_value=self.console
            ),
            patch(
                "gamdl.cli.commands.runner._authenticate",
                new_callable=AsyncMock,
                return_value=(api, None),
            ),
            patch("gamdl.cli.commands.runner.create_drm", new_callable=AsyncMock),
            patch("gamdl.cli.commands.runner.Downloader", return_value=downloader),
            patch("gamdl.cli.commands.runner.MediaResolver", Resolver),
            patch(
                "gamdl.cli.commands.runner.select_stream",
                new_callable=AsyncMock,
                return_value=1,
            ),
            patch("gamdl.cli.commands.runner.interactive", return_value=True),
        ):
            result = await run_rip(
                RipOptions(
                    urls=("https://music.apple.com/br/album/test/123",),
                    output_dir="Test Music",
                    ask_song_codec=True,
                    no_progress=True,
                )
            )
        self.assertEqual(result, 0, self.lines.getvalue())
        self.assertEqual(
            self.lines.getvalue().count("ALAC selected without Wrapper"), 1
        )
