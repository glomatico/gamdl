"""Real Prompt Toolkit input events for every full-screen selection menu."""

import asyncio
import unittest
from io import StringIO
from unittest.mock import Mock

import click
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from gamdl.cli.progress import RipProgress
from gamdl.cli.terminal import TerminalController
from gamdl.cli.tui.components import MenuChoice
from gamdl.cli.tui.multiselect import MultiChoiceState, select_many
from gamdl.cli.tui.selector import select_one, select_stream
from gamdl.download import DownloadEvent, DownloadStage, EventState
from tests.test_cli_prompts import QuestionaryTests


class AlternateScreenOutput(DummyOutput):
    def __init__(self):
        super().__init__()
        self.entered = 0
        self.left = 0
        self.started = asyncio.Event()

    def enter_alternate_screen(self):
        self.entered += 1
        self.started.set()

    def quit_alternate_screen(self):
        self.left += 1


class TuiSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def send_keys(self, coroutine, text: str):
        output = AlternateScreenOutput()
        with create_pipe_input() as pipe:
            task = asyncio.create_task(coroutine(pipe, output))
            try:
                await asyncio.wait_for(output.started.wait(), timeout=3)
                pipe.send_text(text)
                return await asyncio.wait_for(task, timeout=3)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self.assertEqual((output.entered, output.left), (1, 1))

    async def test_all_five_stream_menus_are_full_screen_and_use_arrow_selection(self):
        for request, _name, _detail in QuestionaryTests().requests():
            with self.subTest(type=type(request).__name__):
                # Each request's first candidate uses occurrence index 0.
                answer = await self.send_keys(
                    lambda pipe, out: select_stream(
                        request, title="Test Track", input=pipe, output=out
                    ),
                    "\r",
                )
                self.assertEqual(answer, 0)

    async def test_arrow_down_selects_second_occurrence(self):
        choices = [
            MenuChoice("AAC", 13, "256 kbps"),
            MenuChoice("ALAC", 19, "Lossless"),
        ]
        answer = await self.send_keys(
            lambda pipe, out: select_one(
                "Song codec", choices, subtitle="Billie Jean", input=pipe, output=out
            ),
            "\x1b[B\r",
        )
        self.assertEqual(answer, 19)

    async def test_page_down_and_page_up_navigate_single_choice(self):
        choices = [MenuChoice(f"Option {i}", i + 10) for i in range(30)]
        result = await self.send_keys(
            lambda pipe, out: select_one("Codec", choices, input=pipe, output=out),
            "\x1b[6~\x1b[6~\x1b[5~\r",
        )
        self.assertEqual(result, 20)

    async def test_multiselect_ctrl_a_downloads_in_occurrence_order(self):
        choices = [
            MenuChoice("Track A", 8),
            MenuChoice("Track B", 2),
            MenuChoice("Track C", 5),
        ]
        result = await self.send_keys(
            lambda pipe, out: select_many(
                "Album tracks", choices, input=pipe, output=out
            ),
            "\x01\x1b[F\r",  # Ctrl+A, End, Enter on Download selected
        )
        self.assertEqual(result, [8, 2, 5])

    async def test_multiselect_space_tracks_occurrence_not_title(self):
        choices = [MenuChoice("Same", 4), MenuChoice("Same", 9)]
        result = await self.send_keys(
            lambda pipe, out: select_many(
                "Playlist tracks", choices, input=pipe, output=out
            ),
            " \x1b[F\r",  # First duplicate only, then Download selected
        )
        self.assertEqual(result, [4])

    async def test_multiselect_invert_and_clear(self):
        choices = [MenuChoice("A", 5), MenuChoice("B", 2), MenuChoice("C", 8)]
        result = await self.send_keys(
            lambda pipe, out: select_many("Tracks", choices, input=pipe, output=out),
            "\x01\x04 \x09\x1b[F\r",  # All, clear, select A, invert, download
        )
        self.assertEqual(result, [2, 8])

    async def test_esc_cancels_and_restores_terminal(self):
        with self.assertRaises(click.Abort):
            await self.send_keys(
                lambda pipe, out: select_one(
                    "Song codec", [MenuChoice("AAC", 1)], input=pipe, output=out
                ),
                "\x1b",
            )

    async def test_cancelled_ptk_session_restores_rich_and_releases_terminal(self):
        for cancellation in ("escape", "task"):
            with self.subTest(cancellation=cancellation):
                output = AlternateScreenOutput()
                progress = RipProgress(
                    enabled=True,
                    console=Console(
                        file=StringIO(), force_terminal=True, width=60, height=10
                    ),
                )
                terminal = TerminalController(progress)

                async def prompt(pipe):
                    async with terminal.session():
                        return await terminal.prompt(
                            select_one(
                                "Song codec",
                                [MenuChoice("AAC", 1)],
                                input=pipe,
                                output=output,
                            )
                        )

                async def next_session():
                    async with terminal.session():
                        pass

                with progress, create_pipe_input() as pipe:
                    task = asyncio.create_task(prompt(pipe))
                    try:
                        await asyncio.wait_for(output.started.wait(), timeout=3)
                        self.assertTrue(progress._prompt_hidden)
                        self.assertFalse(progress.progress.live.is_started)
                        if cancellation == "escape":
                            pipe.send_text("\x1b")
                            expected = click.Abort
                        else:
                            task.cancel()
                            expected = asyncio.CancelledError
                        with self.assertRaises(expected):
                            await asyncio.wait_for(task, timeout=3)
                    finally:
                        if not task.done():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                    self.assertEqual((output.entered, output.left), (1, 1))
                    self.assertFalse(progress._prompt_hidden)
                    self.assertTrue(progress.progress.live.is_started)
                    await asyncio.wait_for(next_session(), timeout=1)

    def test_multichoice_state_cannot_toggle_download_action(self):
        state = MultiChoiceState([MenuChoice("Song", 5)])
        state.move(1)
        state.toggle()
        self.assertEqual(state.selected, set())

    def test_rich_progress_is_restarted_even_if_menu_is_cancelled(self):
        progress = RipProgress(enabled=True)
        progress.progress = Mock()
        progress.progress.live.transient = False
        with self.assertRaises(click.Abort):
            with progress.suspend():
                raise click.Abort()
        progress.progress.stop.assert_called_once_with()
        progress.progress.start.assert_called_once_with()
        self.assertIs(progress.progress.live.transient, False)

    def test_consecutive_prompts_do_not_restart_or_leave_progress_frames(self):
        progress = RipProgress(
            enabled=True, console=Console(file=StringIO(), force_terminal=False)
        )
        progress.progress = Mock()
        progress.progress.live.transient = False
        transient_on_stop = []
        progress.progress.stop.side_effect = lambda: transient_on_stop.append(
            progress.progress.live.transient
        )
        with progress:
            progress.hide_for_prompt()
            progress.hide_for_prompt()  # Two full-screen menus in one selection.
            progress.event(
                1,
                "Test",
                DownloadEvent(
                    "111", DownloadStage.FETCHING_STREAMS, EventState.STARTED
                ),
            )
            self.assertEqual(progress.progress.start.call_count, 1)
            self.assertEqual(progress.progress.stop.call_count, 1)
            self.assertIs(progress.progress.live.transient, False)
            progress.event(
                1,
                "Test",
                DownloadEvent(
                    "111", DownloadStage.FETCHING_STREAMS, EventState.COMPLETED
                ),
            )
            # Background stage events never own the terminal.
            self.assertEqual(progress.progress.start.call_count, 1)
            progress.resume_after_prompt()
            self.assertEqual(progress.progress.start.call_count, 2)
            progress.finish(1, "Test", "downloaded")
            self.assertEqual(progress.progress.start.call_count, 2)
        self.assertEqual(transient_on_stop, [True, False])

    def test_prompt_pause_cannot_be_resumed_by_worker_or_discovery(self):
        progress = RipProgress(
            enabled=True, console=Console(file=StringIO(), force_terminal=False)
        )
        progress.progress = Mock()
        progress.progress.live.transient = False
        with progress:
            progress.hide_for_prompt()
            progress.finish(1, "Already downloaded", "skipped")
            self.assertEqual(progress.progress.start.call_count, 1)
            progress.discovery_finished(0)
            self.assertEqual(progress.progress.start.call_count, 1)
            progress.resume_after_prompt()
            self.assertEqual(progress.progress.start.call_count, 2)
