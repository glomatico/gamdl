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
from gamdl.pagination import MediaPage


MAIN = SelectionKind.ARTIST_MAIN_ALBUMS
VIDEOS = SelectionKind.ARTIST_MUSIC_VIDEOS


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []

        async def load(kind, page):
            self.calls.append((kind, page))
            count = 3 if kind == MAIN else 2
            start = page * 2
            return MediaPage(
                tuple(
                    CollectionOption(i, f"Item {i}")
                    for i in range(start, min(start + 2, count))
                ),
                str(start + 2) if start + 2 < count else None,
            )

        self.request = ArtistBrowseRequest(
            source_id="artist123",
            artist_name="Janet",
            categories=((MAIN, "Main Albums"), (VIDEOS, "Music Videos")),
            load_page=load,
        )

    async def test_space_category_and_root_ctrl_a_do_not_fetch(self):
        state = ArtistBrowserState(self.request)
        state.toggle()
        self.assertEqual(state.mark(MAIN), "[x]")
        self.assertEqual(self.calls, [])
        await state.select_all()
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
        self.assertEqual(self.calls, [(MAIN, 0)])
        self.assertEqual(state.category, MAIN)
        self.assertEqual(state.mark_item(0), "[x]")
        state.toggle()  # Exclude first album
        self.assertEqual(state.mark_item(0), "[ ]")
        self.assertIsNone(state.selected[MAIN])
        self.assertEqual(state.excluded[MAIN], {0})
        self.assertTrue(state.back())
        self.assertEqual(state.mark(MAIN), "[-]")
        await state.expand()
        self.assertEqual(self.calls, [(MAIN, 0)])
        await state.select_all()
        self.assertEqual(self.calls, [(MAIN, 0), (MAIN, 1)])
        self.assertEqual(state.result().selections, (ArtistCategorySelection(MAIN),))
        self.assertTrue(state.back())
        state.move(1)
        await state.expand()
        self.assertEqual(self.calls, [(MAIN, 0), (MAIN, 1), (VIDEOS, 0)])
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

    async def test_left_right_pages_preserve_global_indices_and_do_not_refetch(self):
        state = ArtistBrowserState(self.request)
        await state.expand()
        self.assertEqual(state.page_number, 1)
        self.assertEqual([item.index for item in state.items], [0, 1])
        state.toggle()  # First item in first page
        await state.next_page()
        self.assertEqual(state.page_number, 2)
        self.assertEqual([item.index for item in state.items], [2])
        state.toggle()
        self.assertEqual(
            state.result().selections, (ArtistCategorySelection(MAIN, (0, 2)),)
        )
        state.previous_page()
        self.assertEqual(state.page_number, 1)
        self.assertEqual(state.mark_item(0), "[x]")
        await state.next_page()
        self.assertEqual(state.mark_item(2), "[x]")
        self.assertEqual(self.calls, [(MAIN, 0), (MAIN, 1)])
        await state.next_page()  # Last page cannot advance
        self.assertEqual(state.page_number, 2)

    async def test_all_except_one_keeps_unseen_pages_selected(self):
        state = ArtistBrowserState(self.request)
        state.toggle()  # Entire category, no fetch
        await state.expand()
        state.toggle()  # Exclude first item on first page
        await state.next_page()
        self.assertEqual(state.mark_item(2), "[x]")
        self.assertEqual(
            state.result().selections, (ArtistCategorySelection(MAIN, None, (0,)),)
        )

    async def test_ctrl_a_fetches_every_page_and_clears_exclusions(self):
        state = ArtistBrowserState(self.request)
        state.toggle()
        await state.expand()
        state.toggle()  # exclude one
        await state.select_all()
        self.assertEqual(self.calls, [(MAIN, 0), (MAIN, 1)])
        self.assertEqual(state.result().selections, (ArtistCategorySelection(MAIN),))
        self.assertEqual(state.page_number, 1)
        await state.next_page()
        self.assertEqual(self.calls, [(MAIN, 0), (MAIN, 1)])

    async def test_cancelled_load_all_does_not_change_selections(self):
        request = self.request
        in_progress = asyncio.Event()

        async def slow(kind, number):
            if number == 1:
                in_progress.set()
                await asyncio.sleep(30)
            return await request.load_page(kind, number)

        state = ArtistBrowserState(
            ArtistBrowseRequest(
                "artist",
                "Janet",
                request.categories,
                slow,
            )
        )
        await state.expand()
        state.toggle()  # Select one occurrence before Ctrl+A.
        task = asyncio.create_task(state.select_all())
        await asyncio.wait_for(in_progress.wait(), timeout=2)
        task.cancel()
        await task
        self.assertFalse(state.loading)
        self.assertEqual(
            state.result().selections, (ArtistCategorySelection(MAIN, (0,)),)
        )

    async def test_real_ptk_right_left_page_keys_and_selection(self):
        with create_pipe_input() as pipe:
            task = asyncio.create_task(
                browse_artist(self.request, input=pipe, output=DummyOutput())
            )
            await asyncio.sleep(0.07)
            pipe.send_text("\r")  # Expand first category
            await asyncio.sleep(0.12)
            pipe.send_text("\x1b[C")  # Right: next page
            await asyncio.sleep(0.12)
            pipe.send_text(" ")  # Select global occurrence index 2
            pipe.send_text("\x1b[D")  # Left: first page
            await asyncio.sleep(0.08)
            pipe.send_text("\x1b")  # Back to category list
            await asyncio.sleep(0.60)
            pipe.send_text("\x1b[F\r")
            result = await asyncio.wait_for(task, timeout=3)
        self.assertEqual(result.selections, (ArtistCategorySelection(MAIN, (2,)),))
        self.assertEqual(self.calls, [(MAIN, 0), (MAIN, 1)])

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
            self.assertEqual(self.calls, [(MAIN, 0)])
            pipe.send_text("\x01")  # Ctrl+A: mark all in category
            pipe.send_text("\x1b")  # Escape: go back to categories
            await asyncio.sleep(0.60)  # VT100 escape disambiguation timeout
            pipe.send_text("\x1b[F")  # End: Download selected
            pipe.send_text("\r")
            result = await asyncio.wait_for(task, timeout=3)
        self.assertEqual(result.selections, (ArtistCategorySelection(MAIN),))
