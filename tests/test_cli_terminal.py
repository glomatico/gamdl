"""Terminal ownership and bounded Rich progress, including real small TTYs."""

import asyncio
import re
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from gamdl.cli.progress import RipProgress
from gamdl.cli.terminal import TerminalController
from gamdl.cli.tui.selector import select_stream
from gamdl.download import DownloadEvent, DownloadStage, EventState
from gamdl.media import AudioSelectionRequest, MusicVideo, VideoSelectionRequest
from gamdl.stream import StreamProgress
from tests.test_video_stream import AUDIO_PLAYLISTS, MASTER, PAGE, video_api


def video_event(track: str, percent: float) -> DownloadEvent:
    return DownloadEvent(
        "video",
        DownloadStage.DOWNLOADING,
        EventState.PROGRESS,
        track=track,
        progress=StreamProgress(percent, percent, 1, 2, 100, 1.0),
    )


class RecordingTerminalOutput(Vt100_Output):
    """Use PTK's real ANSI renderer against the same sink as Rich."""

    def __init__(self, stream: StringIO):
        super().__init__(
            stream,
            lambda: Size(rows=10, columns=90),
            term="xterm-256color",
            enable_cpr=False,
        )
        self.opened = asyncio.Event()
        self.entered = 0
        self.left = 0

    def enter_alternate_screen(self):
        super().enter_alternate_screen()
        self.entered += 1
        self.opened.set()

    def quit_alternate_screen(self):
        super().quit_alternate_screen()
        self.left += 1


def plain_output(stream: StringIO) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", stream.getvalue())


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_selection_lock_is_exclusive_and_restores_after_cancellation(self):
        console = Console(file=StringIO(), force_terminal=True, width=60, height=10)
        progress = RipProgress(enabled=True, console=console)
        controller = TerminalController(progress)
        second_entered = asyncio.Event()
        started = asyncio.Event()

        async def first():
            async with controller.session():
                await controller.prompt(asyncio.sleep(0))
                started.set()
                await asyncio.Event().wait()

        async def second():
            async with controller.session():
                second_entered.set()
                await controller.prompt(asyncio.sleep(0))

        with progress:
            task1 = asyncio.create_task(first())
            await asyncio.wait_for(started.wait(), 2)
            self.assertTrue(progress._prompt_hidden)
            task2 = asyncio.create_task(second())
            await asyncio.sleep(0.02)
            self.assertFalse(second_entered.is_set())
            task1.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task1
            await asyncio.wait_for(task2, 2)
            self.assertTrue(second_entered.is_set())
            self.assertFalse(progress._prompt_hidden)
            self.assertEqual(progress.progress.live.vertical_overflow, "ellipsis")
            self.assertEqual(
                progress.progress.live._live_render.vertical_overflow, "ellipsis"
            )

    async def test_inactive_worker_does_not_retake_terminal_during_a_menu(self):
        console = Console(file=StringIO(), force_terminal=True, width=60, height=10)
        progress = RipProgress(enabled=True, console=console)
        controller = TerminalController(progress)
        event = asyncio.Event()
        with progress:
            progress.add(1, "Already transferring")
            async with controller.session():

                async def background():
                    progress.finish(1, "Already transferring", "downloaded")
                    event.set()

                await controller.prompt(asyncio.sleep(0))
                worker = asyncio.create_task(background())
                await asyncio.wait_for(event.wait(), 2)
                await worker
                self.assertTrue(progress._prompt_hidden)
                self.assertFalse(progress.progress.live._started)
            await asyncio.sleep(0)
            self.assertFalse(progress._prompt_hidden)
            self.assertTrue(progress.progress.live._started)

    async def test_many_selection_sessions_handoff_without_repainting_rich(self):
        stream = StringIO()
        progress = RipProgress(
            enabled=True,
            console=Console(file=stream, force_terminal=True, width=100, height=10),
        )
        controller = TerminalController(progress)
        first_entered = asyncio.Event()
        allow_first = asyncio.Event()
        active = 0
        peak = 0
        choices = []

        async def choose(index):
            nonlocal active, peak
            async with controller.session():
                active += 1
                peak = max(peak, active)
                try:
                    for phase in ("source", "codec"):

                        async def menu():
                            self.assertTrue(progress._prompt_hidden)
                            self.assertFalse(progress.progress.live._started)
                            choices.append((index, phase))
                            if index == 0 and phase == "source":
                                first_entered.set()
                                await allow_first.wait()
                            await asyncio.sleep(0)

                        await controller.prompt(menu())
                finally:
                    active -= 1

        with patch.object(
            progress.progress, "start", wraps=progress.progress.start
        ) as start:
            with progress:
                progress.add(99, "A transfer that was already running")
                workers = [asyncio.create_task(choose(i)) for i in range(12)]
                try:
                    await asyncio.wait_for(first_entered.wait(), 2)
                    before = stream.getvalue()
                    # This worker is independent of all selection sessions.
                    progress.event(99, "A transfer", video_event("audio", 70))
                    progress.finish(
                        99,
                        "A transfer that was already running",
                        "downloaded",
                        path=Path("/downloads/already-running.m4a"),
                    )
                    self.assertEqual(stream.getvalue(), before)
                    self.assertEqual(start.call_count, 1)
                    allow_first.set()
                    await asyncio.wait_for(asyncio.gather(*workers), 3)
                    await asyncio.sleep(0)
                    self.assertEqual(start.call_count, 2)
                    self.assertTrue(progress.progress.live._started)
                finally:
                    for worker in workers:
                        worker.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
        self.assertEqual(peak, 1)
        self.assertEqual(len(choices), 24)
        for index in range(12):
            at = choices.index((index, "source"))
            self.assertEqual(choices[at + 1], (index, "codec"))
        self.assertEqual(
            plain_output(stream).count("/downloads/already-running.m4a"), 1
        )

    async def test_real_music_video_ask_uses_two_ptk_menus_with_rich_suspended(self):
        stream = StringIO()
        output = RecordingTerminalOutput(stream)
        progress = RipProgress(
            enabled=True,
            console=Console(file=stream, force_terminal=True, width=90, height=10),
        )
        controller = TerminalController(progress)
        media = MusicVideo.from_id("299199411", api=video_api())
        itunes = Mock(get_itunes_page=AsyncMock(return_value=PAGE))
        requests = []

        def respond(request):
            if request.url.path == "/master.m3u8":
                return httpx.Response(200, text=MASTER)
            if request.url.path in AUDIO_PLAYLISTS:
                return httpx.Response(200, text=AUDIO_PLAYLISTS[request.url.path])
            raise AssertionError(f"Unexpected offline resource: {request.url.path}")

        with create_pipe_input() as pipe:

            async def selector(request):
                requests.append(type(request))
                output.opened.clear()

                async def confirm():
                    await output.opened.wait()
                    self.assertTrue(progress._prompt_hidden)
                    self.assertFalse(progress.progress.live._started)
                    pipe.send_text("\r")

                feeder = asyncio.create_task(confirm())
                try:
                    return await controller.prompt(
                        select_stream(
                            request,
                            title="Offline music video",
                            input=pipe,
                            output=output,
                        )
                    )
                finally:
                    feeder.cancel()
                    await asyncio.gather(feeder, return_exceptions=True)

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(respond)
            ) as client:
                with progress:
                    # A repeated ASK must open fresh menus even on the same
                    # real Media instance, whose metadata/transport are cached.
                    for _ in range(2):

                        async def select_tracks():
                            async with controller.session():
                                return await media.get_streams(
                                    ask=True,
                                    selector=selector,
                                    itunes_api=itunes,
                                    client=client,
                                )

                        # Python 3.10's wait_for creates a child task. Acquire
                        # ownership inside that task, just like the CLI worker.
                        selected = await asyncio.wait_for(select_tracks(), 3)
                        self.assertIsNotNone(selected.video)
                        self.assertIsNotNone(selected.audio)
                    await asyncio.sleep(0)
        self.assertEqual(requests, [VideoSelectionRequest, AudioSelectionRequest] * 2)
        self.assertEqual((output.entered, output.left), (4, 4))
        self.assertEqual(stream.getvalue().count("\x1b[?1049h"), 4)
        self.assertEqual(stream.getvalue().count("\x1b[?1049l"), 4)

    async def test_success_history_is_full_buffered_and_once_per_occurrence(self):
        for enabled in (False, True):
            with self.subTest(progress=enabled):
                stream = StringIO()
                progress = RipProgress(
                    enabled=enabled,
                    console=Console(file=stream, force_terminal=False, width=500),
                )
                controller = TerminalController(progress)
                title = "A full [literal] title " + "beyond the live row width " * 5
                paths = [Path(f"/downloads/occurrence-{i}.m4a") for i in range(3)]
                with progress:
                    for i in range(3):
                        progress.add(i, title)
                    async with controller.session():
                        await controller.prompt(asyncio.sleep(0))
                        before = stream.getvalue()
                        for i, status in enumerate(
                            ("downloaded", "overwritten", "skipped")
                        ):
                            progress.finish(i, title, status, path=paths[i])
                            progress.finish(i, title, status, path=paths[i])
                        self.assertEqual(stream.getvalue(), before)
                    await asyncio.sleep(0)
                text = plain_output(stream)
                self.assertEqual(text.count(title), 3)
                for path in paths:
                    self.assertEqual(text.count(str(path)), 1)

    def test_render_recomputes_bounds_on_resize_without_new_download_events(self):
        console = Console(file=StringIO(), force_terminal=True, width=120, height=25)
        progress = RipProgress(enabled=True, max_visible=20, console=console)
        with progress:
            for index in range(20):
                progress.add(index, f"{index}: 漢字 [literal]\nsecond line " * 8)
                progress.event(
                    index,
                    f"{index}: 漢字 [literal]\nsecond line " * 8,
                    DownloadEvent(
                        str(index), DownloadStage.FETCHING_STREAMS, EventState.STARTED
                    ),
                )
            for width, height in ((120, 25), (28, 7), (18, 5), (80, 14)):
                with self.subTest(width=width, height=height):
                    console.width = width
                    console.height = height
                    lines = console.render_lines(
                        progress.progress.get_renderable(),
                        console.options.update(height=None),
                        pad=False,
                    )
                    self.assertLessEqual(len(lines), max(1, height - 2))
                    for line in lines:
                        self.assertLessEqual(
                            sum(segment.cell_length for segment in line), width
                        )

    def test_resuming_rich_does_not_erase_preceding_terminal_history(self):
        stream = StringIO()
        console = Console(file=stream, force_terminal=True, width=80, height=12)
        progress = RipProgress(enabled=True, console=console)
        console.print("Earlier completed downloads")
        with progress:
            for index in range(4):
                progress.add(index, f"Track {index}")
            for _ in range(3):
                progress.hide_for_prompt()
                previous_live = progress.progress.live
                start = len(stream.getvalue())
                progress.resume_after_prompt()
                self.assertIsNot(progress.progress.live, previous_live)
                # First paint starts where the previous transient frame was
                # cleared. Reusing Live would move upward by the old height.
                self.assertNotRegex(stream.getvalue()[start:], r"\x1b\[\d+A")

    def test_finished_rows_move_to_history_and_late_events_do_not_restore_them(self):
        stream = StringIO()
        console = Console(file=stream, force_terminal=False, width=100)
        progress = RipProgress(enabled=True, console=console)
        with progress:
            progress.add(1, "Saved track")
            progress.finish(1, "Saved track", "downloaded", path="/music/saved.m4a")
            progress.event(1, "Saved track", video_event("audio", 25))
            task = next(t for t in progress.progress.tasks if t.id == progress._rows[1])
            self.assertFalse(task.visible)
            self.assertEqual(task.completed, 100)
        self.assertEqual(plain_output(stream).count("/music/saved.m4a"), 1)

    def test_tiny_terminal_bounds_rows_and_video_progress_is_monotonic(self):
        console = Console(file=StringIO(), force_terminal=True, width=50, height=9)
        progress = RipProgress(enabled=True, max_visible=9, console=console)
        with progress:
            for i in range(9):
                progress.add(i, "Long title " * 14)
            tasks = list(progress.progress.tasks)
            self.assertLessEqual(sum(task.visible for task in tasks), 4)
            self.assertLess(len(progress._display_title("X" * 120)), 120)
            progress.add(10, "Music video", tracks=("audio", "video"))
            progress.event(10, "Music video", video_event("audio", 90))
            initial = progress._last_percent[10]
            self.assertEqual(initial, 45)
            progress.event(10, "Music video", video_event("video", 10))
            self.assertEqual(progress._last_percent[10], 50)
            progress.event(10, "Music video", video_event("video", 0))
            self.assertEqual(progress._last_percent[10], 50)
            progress.finish(10, "Music video", "failed")
            task = next(
                t for t in progress.progress.tasks if t.id == progress._rows[10]
            )
            self.assertIsNotNone(task.stop_time)
