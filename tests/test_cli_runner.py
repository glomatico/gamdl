"""Offline CLI orchestration tests: lazy tracks, failures, ordering and fail-fast."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from gamdl.cli.commands.rip import RipOptions
from gamdl.cli.commands.runner import _short_error, run_rip
from gamdl.download import DownloadFinalizationError, DownloadResult, MediaStatus
from gamdl.media import (
    MediaSource,
    MusicVideo,
    MusicVideoCodec,
    Playlist,
    Song,
    SongCodec,
)
from gamdl.api.exceptions import GamdlApiResponseError
from gamdl.media.exceptions import StreamNotAvailableError, UnsupportedMediaError


class CliRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = Mock(active_subscription=True, storefront="br")
        self.api.get_playlist = AsyncMock(
            return_value={
                "data": [{"attributes": {"name": "My Playlist", "curatorName": "DJ"}}]
            }
        )

    async def _execute(
        self,
        *,
        fail_on=None,
        fail_fast=False,
        n=4,
        finalization_failure=False,
        urls=None,
        fail_source=None,
    ):
        songs = []
        playlist = Playlist("playlist-id", api=self.api)
        for index in range(n):
            song = Song.from_id(str(1000 + index), api=self.api)
            song.source = MediaSource("playlist_tracks", "playlist-id", index, playlist)
            song.get_metadata = AsyncMock(
                return_value={"attributes": {"name": f"Song {index}"}}
            )
            songs.append(song)

        class Resolver:
            def __init__(self, api, **kwargs):
                pass

            async def from_url(self, url):
                if url == fail_source:
                    raise UnsupportedMediaError("Bad URL")
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
                if media.id == fail_on and finalization_failure:
                    partial = DownloadResult(
                        media.id,
                        Path(f"{media.id}.m4a"),
                        MediaStatus.DOWNLOADED,
                        1024,
                        SongCodec.AAC,
                    )
                    raise DownloadFinalizationError(
                        partial.path, "lyrics", partial
                    ) from PermissionError("secret path")
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
            urls or ("https://music.apple.com/br/playlist/my-playlist/playlist-id",),
            progress=False,
            save_playlist=True,
            concurrent_downloads=2,
            continue_on_error=not fail_fast,
        )
        with (
            patch(
                "gamdl.cli.commands.runner._authenticate",
                new_callable=AsyncMock,
                return_value=(self.api, None),
            ),
            patch(
                "gamdl.cli.commands.runner.create_drm",
                new_callable=AsyncMock,
                return_value=Mock(),
            ),
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
            self.assertNotIn("playlist_tags", options)
            self.assertTrue(options["save_playlist"])
        self.api.get_playlist.assert_not_awaited()  # CLI never fetches playlist metadata

    async def test_known_track_failure_does_not_abort_playlist(self):
        code, received, _ = await self._execute(fail_on="1001")
        self.assertEqual(code, 1)
        self.assertEqual(len(received), 4)
        self.assertEqual(
            _short_error(StreamNotAvailableError("No stream"))[0],
            "Requested codec or stream unavailable",
        )

    async def test_multiple_urls_share_session_and_aggregate_track_downloads(self):
        code, received, peak = await self._execute(urls=("URL1", "URL2"))
        self.assertEqual(code, 0)
        self.assertEqual(len(received), 8)
        self.assertLessEqual(peak, 2)

    async def test_bad_url_does_not_stop_later_urls_by_default(self):
        code, received, _ = await self._execute(urls=("BAD", "GOOD"), fail_source="BAD")
        self.assertEqual(code, 1)
        self.assertEqual(len(received), 4)

    async def test_fail_fast_bad_url_stops_later_urls(self):
        code, received, _ = await self._execute(
            urls=("BAD", "GOOD"), fail_source="BAD", fail_fast=True
        )
        self.assertEqual(code, 1)
        self.assertEqual(received, [])

    async def test_media_saved_finalization_failure_is_not_a_failed_download(self):
        code, received, _ = await self._execute(
            fail_on="1001", finalization_failure=True
        )
        self.assertEqual(code, 1)
        self.assertEqual(len(received), 4)
        partial = DownloadResult(
            "1001", Path("1001.m4a"), MediaStatus.DOWNLOADED, 1024, SongCodec.AAC
        )
        try:
            raise DownloadFinalizationError(
                partial.path, "lyrics", partial
            ) from PermissionError("private filename")
        except DownloadFinalizationError as exc:
            self.assertEqual(
                _short_error(exc)[0],
                "Media saved, but couldn't save lyrics: permission denied",
            )

    async def test_wrapper_auth_video_initializes_itunes_and_ignores_song_codec(self):
        """Regression: wrapper auth must not prevent iTunes HLS initialization."""
        video = MusicVideo.from_id("1445836237", api=self.api)
        video.get_metadata = AsyncMock(
            return_value={"attributes": {"name": "Rhythm Nation"}}
        )

        class Resolver:
            def __init__(self, api, **kwargs):
                pass

            async def from_url(self, url):
                yield video

        itunes = AsyncMock()
        itunes.__aenter__.return_value = itunes
        downloader = Mock(itunes_api=None)
        downloader.download = AsyncMock(
            return_value=DownloadResult(
                video.id, Path("Rhythm Nation.m4v"), MediaStatus.SKIPPED, 0, None
            )
        )
        wrapper = Mock()
        with (
            patch(
                "gamdl.cli.commands.runner._authenticate",
                new_callable=AsyncMock,
                return_value=(self.api, wrapper),
            ),
            patch(
                "gamdl.cli.commands.runner.create_drm",
                new_callable=AsyncMock,
                return_value=Mock(),
            ),
            patch("gamdl.cli.commands.runner.Downloader", return_value=downloader),
            patch("gamdl.cli.commands.runner.MediaResolver", Resolver),
            patch(
                "gamdl.cli.commands.runner.ItunesApi.create",
                new_callable=AsyncMock,
                return_value=itunes,
            ) as create_itunes,
        ):
            status = await run_rip(
                RipOptions(
                    (
                        "https://music.apple.com/br/music-video/rhythm-nation/1445836237",
                    ),
                    auth_mode="wrapper",
                    song_codec="alac",
                    progress=False,
                )
            )
        self.assertEqual(status, 0)
        create_itunes.assert_awaited_once_with(storefront="br", language="en-US")
        self.assertIs(downloader.itunes_api, itunes)
        kwargs = downloader.download.await_args.kwargs
        self.assertNotIn("codec", kwargs)
        # Direct dataclass construction has no Click default_map; the resolver
        # uses its own H.264/H.265 defaults when priority is None.
        self.assertIsNone(kwargs["codec_priority"])

    async def test_video_falls_back_when_itunes_client_cannot_initialize(self):
        video = MusicVideo.from_id("1445836237", api=self.api)
        video.get_metadata = AsyncMock(
            return_value={"attributes": {"name": "Rhythm Nation"}}
        )

        class Resolver:
            def __init__(self, api, **kwargs):
                pass

            async def from_url(self, url):
                yield video

        downloader = Mock(itunes_api=None)
        downloader.download = AsyncMock(
            return_value=DownloadResult(
                video.id, Path("Rhythm Nation.m4v"), MediaStatus.SKIPPED, 0, None
            )
        )
        with (
            patch(
                "gamdl.cli.commands.runner._authenticate",
                new_callable=AsyncMock,
                return_value=(self.api, Mock()),
            ),
            patch(
                "gamdl.cli.commands.runner.create_drm",
                new_callable=AsyncMock,
                return_value=Mock(),
            ),
            patch("gamdl.cli.commands.runner.Downloader", return_value=downloader),
            patch("gamdl.cli.commands.runner.MediaResolver", Resolver),
            patch(
                "gamdl.cli.commands.runner.ItunesApi.create",
                new_callable=AsyncMock,
                side_effect=GamdlApiResponseError("Storefront lookup unavailable"),
            ),
        ):
            code = await run_rip(
                RipOptions(
                    (
                        "https://music.apple.com/br/music-video/rhythm-nation/1445836237",
                    ),
                    auth_mode="wrapper",
                    progress=False,
                )
            )
        self.assertEqual(code, 0)
        self.assertIsNone(downloader.itunes_api)
        downloader.download.assert_awaited_once()

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
