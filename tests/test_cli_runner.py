"""Offline CLI orchestration tests: lazy tracks, failures, ordering and fail-fast."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from gamdl.cli.commands.rip import RipOptions
from gamdl.cli.commands.runner import _short_error, run_rip
from gamdl.download import DownloadResult, MediaStatus
from gamdl.media import MediaSource, Song, SongCodec
from gamdl.media.exceptions import StreamNotAvailableError


class CliRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = Mock(active_subscription=True, storefront="br")
        self.api.get_playlist = AsyncMock(
            return_value={
                "data": [{"attributes": {"name": "My Playlist", "curatorName": "DJ"}}]
            }
        )

    async def _execute(self, *, fail_on=None, fail_fast=False, n=4):
        songs = []
        for index in range(n):
            song = Song.from_id(str(1000 + index), api=self.api)
            song.source = MediaSource("playlist_tracks", "playlist-id", index)
            song.get_metadata = AsyncMock(
                return_value={"attributes": {"name": f"Song {index}"}}
            )
            songs.append(song)

        class Resolver:
            def __init__(self, api):
                pass

            async def from_url(self, url):
                for song in songs:
                    yield song

        active = 0
        peak = 0
        received = []

        async def fake_download(media, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                received.append((media.id, kwargs))
                await asyncio.sleep(0.003)
                if media.id == fail_on:
                    raise StreamNotAvailableError("Codec unavailable")
                return DownloadResult(
                    media.id,
                    Path(f"{media.id}.m4a"),
                    MediaStatus.DOWNLOADED,
                    1024,
                    SongCodec.AAC,
                )
            finally:
                active -= 1

        fake_downloader = Mock()
        fake_downloader.download = AsyncMock(side_effect=fake_download)
        fake_downloader.itunes_api = None
        opts = RipOptions(
            "https://music.apple.com/br/playlist/my-playlist/playlist-id",
            progress=False,
            concurrent_downloads=2,
            continue_on_error=not fail_fast,
        )
        with (
            patch(
                "gamdl.cli.commands.runner._authenticate",
                new_callable=AsyncMock,
                return_value=(self.api, None),
            ),
            patch("gamdl.cli.commands.runner.Drm.from_widevine", return_value=Mock()),
            patch("gamdl.cli.commands.runner.Downloader", return_value=fake_downloader),
            patch("gamdl.cli.commands.runner.MediaResolver", Resolver),
        ):
            code = await run_rip(opts)
        return code, received, peak

    async def test_lazy_playlist_collection_order_and_bounded_concurrency(self):
        code, received, peak = await self._execute()
        self.assertEqual(code, 0)
        self.assertEqual(len(received), 4)
        self.assertGreaterEqual(peak, 1)
        self.assertLessEqual(peak, 2)
        for index, (media_id, options) in enumerate(received):
            self.assertEqual(options["playlist_tags"].track, index + 1)
            self.assertEqual(options["playlist_tags"].title, "My Playlist")
            self.assertTrue(options["save_playlist"])
        self.api.get_playlist.assert_awaited_once_with("playlist-id", limit_tracks=1)

    async def test_known_track_failure_does_not_abort_playlist(self):
        code, received, _ = await self._execute(fail_on="1001")
        self.assertEqual(code, 1)
        self.assertEqual(len(received), 4)
        self.assertEqual(
            _short_error(StreamNotAvailableError("No stream"))[0],
            "Requested codec or stream unavailable",
        )

    async def test_fail_fast_stops_scheduling_additional_tracks(self):
        code, received, _ = await self._execute(fail_on="1001", fail_fast=True)
        self.assertEqual(code, 1)
        self.assertLess(len(received), 4)

    async def test_rich_progress_adds_rows_and_preserves_failed_percentage(self):
        from gamdl.cli.progress import RipProgress
        from gamdl.download import DownloadEvent, DownloadStage, EventState
        from gamdl.stream import StreamProgress

        with RipProgress(enabled=True, max_visible=1) as ui:
            ui.add(1, "Test [track]")
            ui.event(
                1,
                "Test [track]",
                DownloadEvent(
                    "111",
                    DownloadStage.DOWNLOADING,
                    EventState.PROGRESS,
                    track="audio",
                    progress=StreamProgress(50, 50, 1, 2, 100, 1.0),
                ),
            )
            ui.add(2, "Another")
            ui.finish(1, "Test [track]", "failed", "Codec not available")
            self.assertEqual(round(ui._last_percent[1]), 50)
            ui.finish(2, "Another", "skipped")
            self.assertNotIn(1, ui._rows)
