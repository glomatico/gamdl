"""Offline CLI orchestration tests: lazy tracks, failures, ordering and fail-fast."""

from __future__ import annotations

import asyncio
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import click
from rich.console import Console

from gamdl.api.exceptions import GamdlApiResponseError
from gamdl.cli.commands.rip import RipOptions
from gamdl.cli.commands.runner import _short_error, run_rip
from gamdl.cli.progress import RipProgress
from gamdl.download import (
    DownloadEvent,
    DownloadFinalizationError,
    DownloadResult,
    DownloadStage,
    EventState,
    MediaStatus,
)
from gamdl.media import (
    MediaSource,
    MusicVideo,
    Playlist,
    Song,
    SongCodec,
)
from gamdl.media.exceptions import (
    MediaNotFoundError,
    MediaNotStreamableError,
    StreamNotAvailableError,
    UnsupportedMediaError,
)


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
        unexpected_failure=False,
        debug=False,
        urls=None,
        fail_source=None,
        unavailable_error=None,
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
                if media.id == fail_on and unexpected_failure:
                    raise ValueError("Internal signed URL: sensitive value")
                if media.id == fail_on and unavailable_error is not None:
                    raise unavailable_error
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
            no_progress=False,
            save_playlist=True,
            concurrent_downloads=2,
            fail_fast=fail_fast,
            debug=debug,
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
        # CLI never fetches playlist metadata.
        self.api.get_playlist.assert_not_awaited()

    async def test_known_track_failure_does_not_abort_playlist(self):
        code, received, _ = await self._execute(fail_on="1001")
        self.assertEqual(code, 1)
        self.assertEqual(len(received), 4)
        self.assertEqual(
            _short_error(StreamNotAvailableError("No stream"))[0],
            "Requested codec or stream unavailable",
        )

    async def test_unstreamable_tracks_are_skipped_without_error_or_fail_fast(self):
        for unavailable_error, expected in (
            (MediaNotStreamableError("unavailable"), "Media is not streamable"),
            (MediaNotFoundError("missing"), "Media is unavailable in this storefront"),
        ):
            with self.subTest(kind=type(unavailable_error).__name__):
                buffer = StringIO()
                with patch(
                    "gamdl.cli.commands.runner.output_console",
                    return_value=Console(file=buffer, force_terminal=False),
                ):
                    code, received, _ = await self._execute(
                        fail_on="1001",
                        fail_fast=True,
                        unavailable_error=unavailable_error,
                    )
                self.assertEqual(code, 0)
                self.assertEqual(len(received), 4)
                output = buffer.getvalue()
                self.assertIn(
                    "3 downloaded, 0 overwritten, 1 skipped, 0 failed", output
                )
                self.assertIn(expected, output)
                self.assertNotIn("Errors (", output)

    async def test_errors_report_after_summary_with_opt_in_tracebacks(self):
        for debug in (False, True):
            with self.subTest(debug=debug):
                buffer = StringIO()
                with patch(
                    "gamdl.cli.commands.runner.output_console",
                    return_value=Console(file=buffer, force_terminal=False),
                ):
                    code, _, _ = await self._execute(
                        fail_on="1001", unexpected_failure=True, debug=debug
                    )
                self.assertEqual(code, 1)
                output = buffer.getvalue()
                self.assertLess(output.index("Finished:"), output.index("Errors (1):"))
                self.assertIn("Song 1: Unexpected download error (ValueError)", output)
                self.assertEqual("Traceback (most recent call last)" in output, debug)
                self.assertEqual(
                    "Internal signed URL: sensitive value" in output, debug
                )

    async def test_expected_codec_failures_print_once_without_debug_tracebacks(self):
        buffer = StringIO()
        with patch(
            "gamdl.cli.commands.runner.output_console",
            return_value=Console(file=buffer, force_terminal=False),
        ):
            code, _, _ = await self._execute(fail_on="1001", debug=True)
        self.assertEqual(code, 1)
        output = buffer.getvalue()
        self.assertIn("Finished:", output)
        self.assertIn("Errors (1):", output)
        self.assertEqual(
            output.count("Song 1: Requested codec or stream unavailable"), 1
        )
        self.assertNotIn("Traceback", output)
        self.assertNotIn("review before sharing", output)

    async def test_library_to_two_codec_menus_keeps_progress_paused_until_transfer(
        self,
    ):
        """Never flash Rich between library, source and codec alt-screens."""
        events = []
        song = Song.from_id("1001", api=self.api)
        song.get_metadata = AsyncMock(
            return_value={"attributes": {"name": "Live Track"}}
        )

        class Resolver:
            def __init__(self, api, *, selector, **kwargs):
                self.selector = selector

            async def from_url(self, url):
                await self.selector(object())  # Library category browser.
                yield song

        async def fake_download(media, **kwargs):
            emit = kwargs["event_callback"]
            emit(
                DownloadEvent(
                    media.id, DownloadStage.FETCHING_STREAMS, EventState.STARTED
                )
            )
            await kwargs["selector"](object())  # Song source menu.
            await kwargs["selector"](object())  # Song codec menu.
            emit(
                DownloadEvent(
                    media.id, DownloadStage.FETCHING_STREAMS, EventState.COMPLETED
                )
            )
            return DownloadResult(
                media.id, Path("test.m4a"), MediaStatus.DOWNLOADED, 123, SongCodec.AAC
            )

        async def select_library(_):
            events.append("library-menu")
            return [0]

        async def select_codec(_, *, media_title):
            self.assertEqual(media_title, "Live Track")
            events.append("codec-menu")
            return 0

        console = Console(file=StringIO(), force_terminal=False)
        progress = RipProgress(enabled=True, console=console)
        progress.progress = Mock()
        progress.progress.live.transient = False
        progress.progress.add_task.side_effect = (1, 2)
        progress.progress.start.side_effect = lambda: events.append("start")
        progress.progress.stop.side_effect = lambda: events.append(
            ("stop", progress.progress.live.transient)
        )
        fake_downloader = Mock(itunes_api=None)
        fake_downloader.download = AsyncMock(side_effect=fake_download)

        with (
            patch("gamdl.cli.commands.runner.output_console", return_value=console),
            patch(
                "gamdl.cli.commands.runner._authenticate",
                new_callable=AsyncMock,
                return_value=(self.api, None),
            ),
            patch("gamdl.cli.commands.runner.create_drm", new_callable=AsyncMock),
            patch("gamdl.cli.commands.runner.Downloader", return_value=fake_downloader),
            patch("gamdl.cli.commands.runner.MediaResolver", Resolver),
            patch("gamdl.cli.commands.runner.RipProgress", return_value=progress),
            patch("gamdl.cli.commands.runner.select_media", side_effect=select_library),
            patch("gamdl.cli.commands.runner.select_stream", side_effect=select_codec),
            patch("gamdl.cli.commands.runner.interactive", return_value=True),
        ):
            result = await run_rip(
                RipOptions(
                    ("https://music.apple.com/library",),
                    no_progress=False,
                    ask_song_codec=True,
                )
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            events,
            [
                "start",
                ("stop", True),
                "library-menu",
                "start",
                ("stop", True),
                "codec-menu",
                "codec-menu",
                "start",
                ("stop", False),
            ],
        )

    async def test_interactive_prompts_stay_exclusive_while_downloads_overlap(self):
        """Two transfers continue while a third track's prompt owns the TTY."""
        songs = []
        for n in range(3):
            song = Song.from_id(str(1000 + n), api=self.api)
            song.get_metadata = AsyncMock(
                return_value={"attributes": {"name": f"Song {n}"}}
            )
            songs.append(song)

        class Resolver:
            def __init__(self, api, **kwargs):
                pass

            async def from_url(self, url):
                for song in songs:
                    yield song

        third_prompt = asyncio.Event()
        first_done = asyncio.Event()
        third_selected = asyncio.Event()
        transfers = 0
        maximum = 0
        prompt_count = 0
        simultaneous_prompts = 0
        saw_background_completion = False
        console = Console(file=StringIO(), width=60, height=12, force_terminal=True)
        progress = RipProgress(enabled=True, console=console)

        async def prompt(request, *, media_title):
            nonlocal prompt_count, simultaneous_prompts
            prompt_count += 1
            simultaneous_prompts = max(simultaneous_prompts, 1)
            self.assertTrue(progress._prompt_hidden)
            if media_title == "Song 2":
                third_prompt.set()
                await asyncio.wait_for(first_done.wait(), 2)
                # Track 0 finished in the background, without stealing Rich.
                self.assertTrue(progress._prompt_hidden)
                self.assertFalse(progress.progress.live._started)
            return 0

        async def download(media, **kwargs):
            nonlocal transfers, maximum, saw_background_completion
            callback = kwargs["event_callback"]
            callback(
                DownloadEvent(
                    media.id, DownloadStage.FETCHING_STREAMS, EventState.STARTED
                )
            )
            await kwargs["selector"](object())
            await kwargs["selector"](object())
            callback(
                DownloadEvent(
                    media.id, DownloadStage.FETCHING_STREAMS, EventState.COMPLETED
                )
            )
            transfers += 1
            maximum = max(maximum, transfers)
            try:
                if media.id == "1000":
                    await asyncio.wait_for(third_prompt.wait(), 2)
                    saw_background_completion = progress._prompt_hidden
                    first_done.set()
                elif media.id == "1001":
                    await asyncio.wait_for(third_selected.wait(), 2)
                else:
                    third_selected.set()
                return DownloadResult(
                    media.id,
                    Path(f"{media.id}.m4a"),
                    MediaStatus.DOWNLOADED,
                    123,
                    SongCodec.AAC,
                )
            finally:
                transfers -= 1

        fake_downloader = Mock(itunes_api=None)
        fake_downloader.download = AsyncMock(side_effect=download)
        with (
            patch("gamdl.cli.commands.runner.output_console", return_value=console),
            patch(
                "gamdl.cli.commands.runner._authenticate",
                new_callable=AsyncMock,
                return_value=(self.api, None),
            ),
            patch("gamdl.cli.commands.runner.create_drm", new_callable=AsyncMock),
            patch("gamdl.cli.commands.runner.Downloader", return_value=fake_downloader),
            patch("gamdl.cli.commands.runner.MediaResolver", Resolver),
            patch("gamdl.cli.commands.runner.RipProgress", return_value=progress),
            patch("gamdl.cli.commands.runner.select_stream", side_effect=prompt),
            patch("gamdl.cli.commands.runner.interactive", return_value=True),
        ):
            code = await asyncio.wait_for(
                run_rip(
                    RipOptions(
                        ("https://music.apple.com/br/album/test/111",),
                        no_progress=False,
                        ask_song_codec=True,
                        concurrent_downloads=3,
                    )
                ),
                timeout=5,
            )

        self.assertEqual(code, 0)
        self.assertEqual(prompt_count, 6)
        self.assertEqual(simultaneous_prompts, 1)
        self.assertGreaterEqual(maximum, 2)
        self.assertTrue(saw_background_completion)

    async def _run_workers(
        self,
        download,
        *,
        count=2,
        concurrency=3,
        ask=False,
        fail_fast=False,
        prompt=None,
        media_prompt=None,
        resolver_factory=None,
        urls=None,
    ):
        songs = []
        for index in range(count):
            song = Song.from_metadata(
                {
                    "id": str(1000 + index),
                    "type": "songs",
                    "attributes": {"name": f"Song {index}"},
                },
                api=self.api,
            )
            songs.append(song)

        class Resolver:
            def __init__(self, api, **kwargs):
                pass

            async def from_url(self, url):
                for song in songs:
                    yield song

        downloader = Mock(itunes_api=None)
        downloader.download = AsyncMock(side_effect=download)
        output = StringIO()
        with (
            patch(
                "gamdl.cli.commands.runner.output_console",
                return_value=Console(file=output, force_terminal=False),
            ),
            patch(
                "gamdl.cli.commands.runner._authenticate",
                new_callable=AsyncMock,
                return_value=(self.api, None),
            ),
            patch("gamdl.cli.commands.runner.create_drm", new_callable=AsyncMock),
            patch("gamdl.cli.commands.runner.Downloader", return_value=downloader),
            patch(
                "gamdl.cli.commands.runner.MediaResolver",
                resolver_factory(songs) if resolver_factory else Resolver,
            ),
            patch("gamdl.cli.commands.runner.interactive", return_value=True),
            patch("gamdl.cli.commands.runner.select_stream", side_effect=prompt),
            patch("gamdl.cli.commands.runner.select_media", side_effect=media_prompt),
        ):
            result = await run_rip(
                RipOptions(
                    urls or ("https://music.apple.com/br/album/test/111",),
                    no_progress=False,
                    ask_song_codec=ask,
                    concurrent_downloads=concurrency,
                    fail_fast=fail_fast,
                )
            )
        return result, output.getvalue()

    async def test_codec_abort_does_not_open_queued_collection_menu(self):
        stream_open = asyncio.Event()
        collection_waiting = asyncio.Event()
        discovery_closed = asyncio.Event()
        collection_prompt = AsyncMock(return_value=[])

        def resolver_factory(songs):
            class Resolver:
                def __init__(self, api, *, selector, **kwargs):
                    self.selector = selector

                async def from_url(self, url):
                    if url == "first":
                        yield songs[0]
                    else:
                        await stream_open.wait()
                        collection_waiting.set()
                        try:
                            await self.selector(object())
                        finally:
                            discovery_closed.set()

            return Resolver

        async def prompt(request, *, media_title):
            stream_open.set()
            await collection_waiting.wait()
            raise click.Abort()

        async def download(media, **kwargs):
            await kwargs["selector"](object())

        with self.assertRaises(click.Abort):
            await asyncio.wait_for(
                self._run_workers(
                    download,
                    ask=True,
                    prompt=prompt,
                    media_prompt=collection_prompt,
                    resolver_factory=resolver_factory,
                    urls=("first", "second"),
                ),
                2,
            )
        collection_prompt.assert_not_awaited()
        self.assertTrue(discovery_closed.is_set())

    async def test_fail_fast_interrupts_open_collection_and_sibling_download(self):
        collection_open = asyncio.Event()
        collection_closed = asyncio.Event()
        sibling_started = asyncio.Event()
        sibling_closed = asyncio.Event()
        discovery_closed = asyncio.Event()

        def resolver_factory(songs):
            class Resolver:
                def __init__(self, api, *, selector, **kwargs):
                    self.selector = selector

                async def from_url(self, url):
                    try:
                        for song in songs:
                            yield song
                        await self.selector(object())
                    finally:
                        discovery_closed.set()

            return Resolver

        async def collection_prompt(request):
            collection_open.set()
            try:
                await asyncio.Event().wait()
            finally:
                collection_closed.set()

        async def download(media, **kwargs):
            if media.id == "1000":
                await collection_open.wait()
                await sibling_started.wait()
                raise StreamNotAvailableError("Unavailable")
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                sibling_closed.set()

        code, output = await asyncio.wait_for(
            self._run_workers(
                download,
                fail_fast=True,
                media_prompt=collection_prompt,
                resolver_factory=resolver_factory,
            ),
            2,
        )
        self.assertEqual(code, 1)
        self.assertTrue(collection_closed.is_set())
        self.assertTrue(discovery_closed.is_set())
        self.assertTrue(sibling_closed.is_set())
        self.assertEqual(
            output.count("Song 0: Requested codec or stream unavailable"), 1
        )
        self.assertNotIn("Traceback", output)

    async def test_collection_abort_does_not_open_queued_codec_menu(self):
        worker_waiting = asyncio.Event()
        stream_prompt = AsyncMock(return_value=0)

        def resolver_factory(songs):
            class Resolver:
                def __init__(self, api, *, selector, **kwargs):
                    self.selector = selector

                async def from_url(self, url):
                    yield songs[0]
                    await self.selector(object())

            return Resolver

        async def collection_prompt(request):
            await worker_waiting.wait()
            raise click.Abort()

        async def download(media, **kwargs):
            worker_waiting.set()
            await kwargs["selector"](object())

        with self.assertRaises(click.Abort):
            await asyncio.wait_for(
                self._run_workers(
                    download,
                    ask=True,
                    prompt=stream_prompt,
                    media_prompt=collection_prompt,
                    resolver_factory=resolver_factory,
                ),
                2,
            )
        stream_prompt.assert_not_awaited()

    async def test_worker_abort_interrupts_pending_discovery_request(self):
        fetching = asyncio.Event()
        discovery_closed = asyncio.Event()

        def resolver_factory(songs):
            class Resolver:
                def __init__(self, api, **kwargs):
                    pass

                async def from_url(self, url):
                    try:
                        yield songs[0]
                        fetching.set()
                        await asyncio.Event().wait()
                    finally:
                        discovery_closed.set()

            return Resolver

        async def download(media, **kwargs):
            await fetching.wait()
            raise click.Abort()

        with self.assertRaises(click.Abort):
            await asyncio.wait_for(
                self._run_workers(download, resolver_factory=resolver_factory), 2
            )
        self.assertTrue(discovery_closed.is_set())

    async def test_abort_during_metadata_lookup_closes_suspended_source(self):
        metadata_started = asyncio.Event()
        metadata_closed = asyncio.Event()
        source_closed = asyncio.Event()
        sources = []

        async def metadata():
            metadata_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                metadata_closed.set()

        def resolver_factory(songs):
            songs[1].get_metadata = AsyncMock(side_effect=metadata)

            async def items():
                try:
                    for song in songs:
                        yield song
                finally:
                    await asyncio.sleep(0)
                    source_closed.set()

            class Resolver:
                def __init__(self, api, **kwargs):
                    pass

                def from_url(self, url):
                    source = items()
                    sources.append(source)  # Don't rely on garbage collection.
                    return source

            return Resolver

        async def download(media, **kwargs):
            await metadata_started.wait()
            raise click.Abort()

        with self.assertRaises(click.Abort):
            await asyncio.wait_for(
                self._run_workers(download, resolver_factory=resolver_factory), 2
            )
        self.assertTrue(metadata_closed.is_set())
        self.assertTrue(source_closed.is_set())
        self.assertTrue(all(source.ag_frame is None for source in sources))

    async def test_recoverable_worker_failure_keeps_collection_menu_open(self):
        collection_open = asyncio.Event()
        worker_failed = asyncio.Event()
        collection_finished = asyncio.Event()

        def resolver_factory(songs):
            class Resolver:
                def __init__(self, api, *, selector, **kwargs):
                    self.selector = selector

                async def from_url(self, url):
                    yield songs[0]
                    await self.selector(object())
                    yield songs[1]

            return Resolver

        async def collection_prompt(request):
            collection_open.set()
            await worker_failed.wait()
            collection_finished.set()
            return []

        async def download(media, **kwargs):
            if media.id == "1000":
                await collection_open.wait()
                worker_failed.set()
                raise StreamNotAvailableError("Unavailable")
            return DownloadResult(
                media.id,
                Path(f"{media.id}.m4a"),
                MediaStatus.DOWNLOADED,
                123,
                SongCodec.AAC,
            )

        code, output = await asyncio.wait_for(
            self._run_workers(
                download,
                media_prompt=collection_prompt,
                resolver_factory=resolver_factory,
            ),
            2,
        )
        self.assertEqual(code, 1)
        self.assertTrue(collection_finished.is_set())
        self.assertIn("1 downloaded", output)
        self.assertIn("1 failed", output)

    async def test_many_ask_workers_prepare_and_transfer_concurrently(self):
        count = 12
        prepared = 0
        transfers = 0
        all_prepared = asyncio.Event()
        all_transferring = asyncio.Event()
        active_prompts = 0
        prompt_order = []

        async def prompt(request, *, media_title):
            nonlocal active_prompts
            active_prompts += 1
            try:
                self.assertEqual(active_prompts, 1)
                prompt_order.append(media_title)
                await asyncio.sleep(0)
                self.assertEqual(active_prompts, 1)
                return 0
            finally:
                active_prompts -= 1

        async def download(media, **kwargs):
            nonlocal prepared, transfers
            # Tag/path preparation happens before any prompt. Holding the
            # selection lease around the whole download deadlocks this gate.
            prepared += 1
            if prepared == count:
                all_prepared.set()
            await all_prepared.wait()
            emit = kwargs["event_callback"]
            emit(
                DownloadEvent(
                    media.id, DownloadStage.FETCHING_STREAMS, EventState.STARTED
                )
            )
            await kwargs["selector"](object())
            await asyncio.sleep(0)  # Manifest discovery between related menus.
            await kwargs["selector"](object())
            emit(
                DownloadEvent(
                    media.id, DownloadStage.FETCHING_STREAMS, EventState.COMPLETED
                )
            )
            transfers += 1
            if transfers == count:
                all_transferring.set()
            await all_transferring.wait()
            return DownloadResult(
                media.id,
                Path(f"{media.id}.m4a"),
                MediaStatus.DOWNLOADED,
                123,
                SongCodec.AAC,
            )

        code, output = await asyncio.wait_for(
            self._run_workers(
                download,
                count=count,
                concurrency=count,
                ask=True,
                prompt=prompt,
            ),
            5,
        )
        self.assertEqual(code, 0)
        self.assertEqual(transfers, count)
        self.assertEqual(len(prompt_order), count * 2)
        for index in range(0, len(prompt_order), 2):
            self.assertEqual(prompt_order[index], prompt_order[index + 1])
        self.assertIn("12 downloaded", output)

    async def test_abort_in_final_batch_cancels_other_workers(self):
        sibling_started = asyncio.Event()
        sibling_cancelled = asyncio.Event()

        async def download(media, **kwargs):
            if media.id == "1000":
                await sibling_started.wait()
                raise click.Abort()
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                sibling_cancelled.set()

        with self.assertRaises(click.Abort):
            await asyncio.wait_for(self._run_workers(download), 2)
        self.assertTrue(sibling_cancelled.is_set())

    async def test_fail_fast_in_final_batch_cancels_other_workers(self):
        sibling_started = asyncio.Event()
        sibling_cancelled = asyncio.Event()

        async def download(media, **kwargs):
            if media.id == "1000":
                await sibling_started.wait()
                raise StreamNotAvailableError("Unavailable")
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                sibling_cancelled.set()

        code, output = await asyncio.wait_for(
            self._run_workers(download, fail_fast=True), 2
        )
        self.assertEqual(code, 1)
        self.assertTrue(sibling_cancelled.is_set())
        self.assertEqual(
            output.count("Song 0: Requested codec or stream unavailable"), 1
        )

    async def test_external_cancellation_closes_active_prompt_and_waiting_worker(self):
        prompt_started = asyncio.Event()
        prompt_closed = asyncio.Event()
        worker_tasks = []

        async def prompt(request, *, media_title):
            prompt_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                prompt_closed.set()

        async def download(media, **kwargs):
            worker_tasks.append(asyncio.current_task())
            await kwargs["selector"](object())

        task = asyncio.create_task(self._run_workers(download, ask=True, prompt=prompt))
        await asyncio.wait_for(prompt_started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertTrue(prompt_closed.is_set())
        self.assertEqual(len(worker_tasks), 2)
        self.assertTrue(all(worker.done() for worker in worker_tasks))

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
                    no_progress=False,
                )
            )
        self.assertEqual(status, 0)
        create_itunes.assert_awaited_once_with(storefront="br", language="en-US")
        self.assertIs(downloader.itunes_api, itunes)
        kwargs = downloader.download.await_args.kwargs
        self.assertNotIn("codec", kwargs)
        self.assertEqual(kwargs["video_extension"], ".m4v")
        # Direct dataclass construction has no Click default_map; the resolver
        # uses its own H.264/H.265 defaults when priority is None.
        self.assertIsNone(kwargs["codec_priority"])

    async def test_music_video_container_mp4_reaches_downloader(self):
        video = MusicVideo.from_id("1445836237", api=self.api)
        video.get_metadata = AsyncMock(
            return_value={"attributes": {"name": "Rhythm Nation"}}
        )

        class Resolver:
            def __init__(self, api, **kwargs):
                pass

            async def from_url(self, url):
                yield video

        downloader = Mock(itunes_api=Mock())
        downloader.download = AsyncMock(
            return_value=DownloadResult(
                video.id, Path("Rhythm Nation.mp4"), MediaStatus.DOWNLOADED, 1024, None
            )
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
            patch("gamdl.cli.commands.runner.Downloader", return_value=downloader),
            patch("gamdl.cli.commands.runner.MediaResolver", Resolver),
        ):
            status = await run_rip(
                RipOptions(
                    ("https://music.apple.com/br/music-video/example/1445836237",),
                    music_video_container="mp4",
                    no_progress=True,
                )
            )
        self.assertEqual(status, 0)
        self.assertEqual(
            downloader.download.await_args.kwargs["video_extension"], ".mp4"
        )

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
                    no_progress=False,
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
