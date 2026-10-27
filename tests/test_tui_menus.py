"""Real Prompt Toolkit input events for every full-screen selection menu."""

import asyncio
import unittest
from unittest.mock import Mock

import click
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from gamdl.cli.tui.components import MenuChoice
from gamdl.cli.tui.multiselect import MultiChoiceState, select_many
from gamdl.cli.tui.selector import select_one, select_stream
from gamdl.cli.progress import RipProgress
from tests.test_cli_prompts import QuestionaryTests


class AlternateScreenOutput(DummyOutput):
    def __init__(self):
        super().__init__()
        self.entered = 0
        self.left = 0

    def enter_alternate_screen(self):
        self.entered += 1

    def quit_alternate_screen(self):
        self.left += 1


class TuiSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def send_keys(self, coroutine, text: str):
        output = AlternateScreenOutput()
        with create_pipe_input() as pipe:
            task = asyncio.create_task(coroutine(pipe, output))
            await asyncio.sleep(0.07)
            pipe.send_text(text)
            value = await asyncio.wait_for(task, timeout=3)
        self.assertEqual((output.entered, output.left), (1, 1))
        return value

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

    async def test_esc_cancels_and_restores_terminal(self):
        with self.assertRaises(click.Abort):
            await self.send_keys(
                lambda pipe, out: select_one(
                    "Song codec", [MenuChoice("AAC", 1)], input=pipe, output=out
                ),
                "\x1b",
            )

    def test_multichoice_state_cannot_toggle_download_action(self):
        state = MultiChoiceState([MenuChoice("Song", 5)])
        state.move(1)
        state.toggle()
        self.assertEqual(state.selected, set())

    def test_rich_progress_is_restarted_even_if_menu_is_cancelled(self):
        progress = RipProgress(enabled=True)
        progress.progress = Mock()
        with self.assertRaises(click.Abort):
            with progress.suspend():
                raise click.Abort()
        progress.progress.stop.assert_called_once_with()
        progress.progress.start.assert_called_once_with()
