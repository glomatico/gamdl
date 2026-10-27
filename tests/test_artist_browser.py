"""Hierarchical artist selection, category caching, and PTK key handling."""

import asyncio
import unittest

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from gamdl.cli.tui.browser import ArtistBrowserState, browse_artist
from gamdl.media import (
    ArtistBrowseRequest,
    ArtistCategorySelection,
    CollectionOption,
    SelectionKind,
)


MAIN = SelectionKind.ARTIST_MAIN_ALBUMS
VIDEOS = SelectionKind.ARTIST_MUSIC_VIDEOS


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []

        async def load(kind):
            self.calls.append(kind)
            return tuple(
                CollectionOption(i, f"Item {i}")
                for i in range(3 if kind == MAIN else 2)
            )

        self.request = ArtistBrowseRequest(
            source_id="artist123",
            artist_name="Janet",
            categories=((MAIN, "Main Albums"), (VIDEOS, "Music Videos")),
            load_items=load,
        )

    async def test_space_category_and_root_ctrl_a_do_not_fetch(self):
        state = ArtistBrowserState(self.request)
        state.toggle()
        self.assertEqual(state.mark(MAIN), "[x]")
        self.assertEqual(self.calls, [])
        state.select_all()
        self.assertEqual(state.mark(VIDEOS), "[x]")
        self.assertEqual(self.calls, [])
        self.assertEqual(
            state.result().selections,
            (ArtistCategorySelection(MAIN), ArtistCategorySelection(VIDEOS)),
        )

    async def test_expand_selection_back_and_reload_from_cache(self):
        state = ArtistBrowserState(self.request)
        state.toggle()  # Select all without loading
        await state.expand()
        self.assertEqual(self.calls, [MAIN])
        self.assertEqual(state.category, MAIN)
        self.assertEqual(state.mark_item(0), "[x]")
        state.toggle()  # Exclude first album
        self.assertEqual(state.mark_item(0), "[ ]")
        self.assertEqual(state.selected[MAIN], {1, 2})
        self.assertTrue(state.back())
        self.assertEqual(state.mark(MAIN), "[-]")
        await state.expand()
        self.assertEqual(self.calls, [MAIN])
        state.select_all()
        self.assertEqual(state.result().selections, (ArtistCategorySelection(MAIN),))
        self.assertTrue(state.back())
        state.move(1)
        await state.expand()
        self.assertEqual(self.calls, [MAIN, VIDEOS])
        state.toggle()  # Only first music video
        self.assertEqual(
            state.result().selections,
            (ArtistCategorySelection(MAIN), ArtistCategorySelection(VIDEOS, (0,))),
        )

    async def test_no_selection_and_back_from_root(self):
        state = ArtistBrowserState(self.request)
        self.assertEqual(state.result().selections, ())
        self.assertFalse(state.back())
        state.move(100)
        self.assertEqual(state.cursor, state.rows - 1)
        state.toggle()  # Download button is not selectable
        self.assertEqual(state.result().selections, ())

    async def test_real_ptk_keyboard_space_enter_and_download_action(self):
        """Exercise the terminal event loop rather than just calling state methods."""
        with create_pipe_input() as pipe:
            task = asyncio.create_task(
                browse_artist(self.request, input=pipe, output=DummyOutput())
            )
            await asyncio.sleep(0.07)
            pipe.send_text(" ")  # Space: whole category, without API calls
            await asyncio.sleep(0.03)
            self.assertEqual(self.calls, [])
            pipe.send_text("\r")  # Enter: expand, not download
            await asyncio.sleep(0.12)
            self.assertEqual(self.calls, [MAIN])
            pipe.send_text("\x01")  # Ctrl+A: mark all in category
            pipe.send_text("\x1b")  # Escape: go back to categories
            await asyncio.sleep(0.60)  # VT100 escape disambiguation timeout
            pipe.send_text("\x1b[F")  # End: Download selected
            pipe.send_text("\r")
            result = await asyncio.wait_for(task, timeout=3)
        self.assertEqual(result.selections, (ArtistCategorySelection(MAIN),))
