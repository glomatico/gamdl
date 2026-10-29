"""Full-screen multi-category search, navigation, and recoverable API failures."""

import asyncio
import unittest

import click
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from gamdl.cli.tui.search import SearchBrowserState, select_search_results
from gamdl.pagination import MediaPage, PageCache


def item(identifier):
    return {"id": identifier, "attributes": {"name": identifier}}


class TrackingOutput(DummyOutput):
    def __init__(self):
        super().__init__()
        self.entered = 0
        self.left = 0

    def enter_alternate_screen(self):
        self.entered += 1

    def quit_alternate_screen(self):
        self.left += 1


class SearchBrowserStateTests(unittest.TestCase):
    def test_categories_are_navigation_not_bulk_selection(self):
        state = SearchBrowserState(
            {
                "songs": PageCache(lambda _: None),
                "music-videos": PageCache(lambda _: None),
            }
        )
        self.assertEqual(state.rows, 3)
        state.toggle()  # Root never selects an entire category.
        self.assertEqual(state.result(), {})
        self.assertEqual(state.total_selected, 0)

        state.caches["songs"].pages.append(MediaPage((item("one"), item("two"))))
        state.open_category("songs")
        state.toggle()
        self.assertEqual(state.result(), {"songs": (0,)})
        self.assertTrue(state.back())
        self.assertIsNone(state.category)
        self.assertEqual(state.total_selected, 1)

    def test_back_to_types_action_retains_active_page_and_selections(self):
        cache = PageCache(lambda _: None)
        cache.pages.extend(
            (MediaPage((item("first"),), "page2"), MediaPage((item("second"),)))
        )
        state = SearchBrowserState({"songs": cache})
        state.pages["songs"] = 1
        state.open_category("songs")
        state.toggle()
        state.cursor = 1  # Back to media types action, even on page 2.
        state.back_to_categories()
        self.assertIsNone(state.category)
        state.open_category("songs")
        self.assertEqual(state.pages["songs"], 1)
        self.assertEqual(state.result(), {"songs": (1,)})


class SearchBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_select_across_types_then_download_from_root(self):
        calls = {"songs": [], "music-videos": [], "uploaded-videos": []}

        def cache(kind):
            async def load(cursor):
                calls[kind].append(cursor)
                return MediaPage((item(kind + "1"), item(kind + "2")))

            return PageCache(load)

        caches = {kind: cache(kind) for kind in calls}
        output = TrackingOutput()
        with create_pipe_input() as pipe:
            task = asyncio.create_task(
                select_search_results(caches, "test", input=pipe, output=output)
            )
            await asyncio.sleep(0.08)
            self.assertEqual(
                calls, {"songs": [], "music-videos": [], "uploaded-videos": []}
            )

            pipe.send_text("\r")  # Expand Songs.
            await asyncio.sleep(0.09)
            pipe.send_text(" \x7f")  # Select one song, return to media types.
            await asyncio.sleep(0.09)
            pipe.send_text("\x1b[B\r")  # Expand Music videos.
            await asyncio.sleep(0.09)
            pipe.send_text("\x1b[B \x7f")  # Select second video, back.
            await asyncio.sleep(0.09)
            pipe.send_text("\x1b[B\x1b[B\r")  # Download selected at root.
            result = await asyncio.wait_for(task, 3)

        self.assertEqual(result, {"songs": (0,), "music-videos": (1,)})
        self.assertEqual(calls["songs"], [None])
        self.assertEqual(calls["music-videos"], [None])
        self.assertEqual(calls["uploaded-videos"], [])
        self.assertEqual((output.entered, output.left), (1, 1))

    async def test_reopening_category_preserves_selections_and_cached_pages(self):
        calls = []

        async def load(cursor):
            calls.append(cursor)
            if cursor is None:
                return MediaPage((item("first"),), "page2")
            return MediaPage((item("second"),))

        caches = {"songs": PageCache(load)}
        with create_pipe_input() as pipe:
            task = asyncio.create_task(
                select_search_results(caches, "test", input=pipe, output=DummyOutput())
            )
            await asyncio.sleep(0.08)
            pipe.send_text("\r")
            await asyncio.sleep(0.09)
            pipe.send_text(" \x1b[C")  # Select first, go to page 2.
            await asyncio.sleep(0.09)
            pipe.send_text(" \x7f\x7f")  # Select second, return to root.
            await asyncio.sleep(0.09)
            pipe.send_text("\r")  # Reopen Songs (cached).
            await asyncio.sleep(0.09)
            pipe.send_text("\x1b[C")  # Cached next page.
            await asyncio.sleep(0.09)
            pipe.send_text("\x7f\x7f")  # Return to root.
            await asyncio.sleep(0.09)
            pipe.send_text("\x1b[B\r")  # Download.
            result = await asyncio.wait_for(task, 3)
        self.assertEqual(result, {"songs": (0, 1)})
        self.assertEqual(calls, [None, "page2"])

    async def test_initial_page_rate_limit_is_recoverable_in_full_screen(self):
        attempts = 0

        async def load(cursor):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                error = RuntimeError("Rate limited")
                error.status_code = 429
                raise error
            return MediaPage((item("song"),))

        with create_pipe_input() as pipe:
            task = asyncio.create_task(
                select_search_results(
                    {"songs": PageCache(load)}, "test", input=pipe, output=DummyOutput()
                )
            )
            await asyncio.sleep(0.08)
            pipe.send_text("\r")
            await asyncio.sleep(0.1)
            self.assertFalse(task.done())
            self.assertEqual(attempts, 1)
            pipe.send_text("\r")  # Retry on same root menu.
            await asyncio.sleep(0.09)
            pipe.send_text(" \x7f\x1b[B\r")
            result = await asyncio.wait_for(task, 3)
        self.assertEqual(result, {"songs": (0,)})
        self.assertEqual(attempts, 2)

    async def test_next_page_rate_limit_and_retry(self):
        attempts = 0

        async def load(cursor):
            nonlocal attempts
            if cursor is None:
                return MediaPage((item("first"),), "page2")
            attempts += 1
            if attempts == 1:
                error = RuntimeError("Rate limited")
                error.status_code = 429
                raise error
            return MediaPage((item("second"),))

        with create_pipe_input() as pipe:
            task = asyncio.create_task(
                select_search_results(
                    {"songs": PageCache(load)}, "test", input=pipe, output=DummyOutput()
                )
            )
            await asyncio.sleep(0.08)
            pipe.send_text("\r")
            await asyncio.sleep(0.09)
            pipe.send_text("\x1b[C")
            await asyncio.sleep(0.09)
            self.assertFalse(task.done())
            pipe.send_text("\x1b[C")
            await asyncio.sleep(0.09)
            pipe.send_text(" \x7f\x7f\x1b[B\r")
            result = await asyncio.wait_for(task, 3)
        self.assertEqual(result, {"songs": (1,)})
        self.assertEqual(attempts, 2)

    async def test_ctrl_a_never_selects_all_search_results(self):
        async def load(cursor):
            return MediaPage((item("one"), item("two")))

        with create_pipe_input() as pipe:
            task = asyncio.create_task(
                select_search_results(
                    {"songs": PageCache(load)}, "test", input=pipe, output=DummyOutput()
                )
            )
            await asyncio.sleep(0.08)
            pipe.send_text("\x01")  # Ctrl+A at root must not select the category.
            pipe.send_text("\r")
            await asyncio.sleep(0.1)
            pipe.send_text("\x01")  # Ctrl+A inside results must not select every item.
            pipe.send_text("\x1b[B\x1b[B\x1b[B\r")  # Attempt Download selected.
            await asyncio.sleep(0.09)
            self.assertFalse(task.done())
            pipe.send_text("\x1b[A\x1b[A\x1b[A \x1b[B\x1b[B\x1b[B\r")
            result = await asyncio.wait_for(task, 3)
        self.assertEqual(result, {"songs": (0,)})

    async def test_empty_category_does_not_end_search(self):
        async def empty(cursor):
            return MediaPage(())

        async def video(cursor):
            return MediaPage((item("video"),))

        with create_pipe_input() as pipe:
            task = asyncio.create_task(
                select_search_results(
                    {"songs": PageCache(empty), "music-videos": PageCache(video)},
                    "test",
                    input=pipe,
                    output=DummyOutput(),
                )
            )
            await asyncio.sleep(0.08)
            pipe.send_text("\r")
            await asyncio.sleep(0.09)
            pipe.send_text("\x7f\x1b[B\r")
            await asyncio.sleep(0.09)
            pipe.send_text(" \x7f\x1b[B\r")
            result = await asyncio.wait_for(task, 3)
        self.assertEqual(result, {"music-videos": (0,)})

    async def test_explicit_type_starts_with_results_and_supports_retry(self):
        attempts = 0

        async def load(cursor):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                error = RuntimeError("Rate limited")
                error.status_code = 429
                raise error
            return MediaPage((item("one"),))

        with create_pipe_input() as pipe:
            task = asyncio.create_task(
                select_search_results(
                    {"songs": PageCache(load)},
                    "test",
                    media_type="songs",
                    input=pipe,
                    output=DummyOutput(),
                )
            )
            await asyncio.sleep(0.1)
            self.assertFalse(task.done())
            pipe.send_text("\r")  # Retry first page from inside the browser.
            await asyncio.sleep(0.09)
            pipe.send_text(" \x1b[B\r")
            result = await asyncio.wait_for(task, 3)
        self.assertEqual(result, {"songs": (0,)})
        self.assertEqual(attempts, 2)

    async def test_cancel_during_loading_does_not_leave_background_tasks(self):
        cancelled = asyncio.Event()

        async def load(cursor):
            if cursor is None:
                return MediaPage((item("first"),), "page2")
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with create_pipe_input() as pipe:
            task = asyncio.create_task(
                select_search_results(
                    {"songs": PageCache(load)}, "test", input=pipe, output=DummyOutput()
                )
            )
            await asyncio.sleep(0.08)
            pipe.send_text("\r")
            await asyncio.sleep(0.09)
            pipe.send_text("\x1b[C")
            await asyncio.sleep(0.09)
            pipe.send_text("\x7f")  # Cancel the page request.
            await asyncio.wait_for(cancelled.wait(), 2)
            pipe.send_text("\x03")  # Ctrl+C cancels the search.
            with self.assertRaises(click.Abort):
                await asyncio.wait_for(task, 3)

    async def test_explicit_type_escape_cancels_initial_loading(self):
        started = asyncio.Event()
        finished = asyncio.Event()

        async def slow(cursor):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        with create_pipe_input() as pipe:
            task = asyncio.create_task(
                select_search_results(
                    {"songs": PageCache(slow)},
                    "test",
                    media_type="songs",
                    input=pipe,
                    output=DummyOutput(),
                )
            )
            await asyncio.wait_for(started.wait(), 2)
            pipe.send_text("\x1b")
            with self.assertRaises(click.Abort):
                await asyncio.wait_for(task, 3)
            self.assertTrue(finished.is_set())
