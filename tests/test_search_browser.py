"""Interactive search pagination, navigation and failed fetch recovery."""

import asyncio
import unittest

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from gamdl.cli.tui.search import select_search_results
from gamdl.pagination import MediaPage, PageCache


class SearchBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_backspace_and_left_navigate_cached_pages(self):
        calls = []

        async def load(cursor):
            calls.append(cursor)
            if cursor is None:
                return MediaPage(
                    ({"id": "1", "attributes": {"name": "First"}},), "page2"
                )
            return MediaPage(({"id": "2", "attributes": {"name": "Second"}},))

        cache = PageCache(load)
        with create_pipe_input() as pipe:
            task = asyncio.create_task(
                select_search_results(cache, "test", input=pipe, output=DummyOutput())
            )
            await asyncio.sleep(0.08)
            pipe.send_text("\x1b[C")  # page 2
            await asyncio.sleep(0.08)
            pipe.send_text("\x7f")  # backspace to page 1
            await asyncio.sleep(0.08)
            pipe.send_text("\x1b[C")  # page 2 (cached)
            await asyncio.sleep(0.08)
            pipe.send_text("\x1b[D")  # left to page 1
            await asyncio.sleep(0.08)
            pipe.send_text(" \x1b[B\r")  # select first result, download
            result = await asyncio.wait_for(task, 2)
        self.assertEqual(result, [0])
        self.assertEqual(calls, [None, "page2"])

    async def test_rate_limit_keeps_menu_alive_and_allows_retry(self):
        attempts = 0

        async def load(cursor):
            nonlocal attempts
            if cursor is None:
                return MediaPage(
                    ({"id": "1", "attributes": {"name": "First"}},), "page2"
                )
            attempts += 1
            if attempts == 1:
                error = RuntimeError("Rate limited")
                error.status_code = 429
                raise error
            return MediaPage(({"id": "2", "attributes": {"name": "Second"}},))

        with create_pipe_input() as pipe:
            task = asyncio.create_task(
                select_search_results(
                    PageCache(load), "test", input=pipe, output=DummyOutput()
                )
            )
            await asyncio.sleep(0.08)
            pipe.send_text("\x1b[C")
            await asyncio.sleep(0.08)
            self.assertFalse(task.done())
            pipe.send_text("\x1b[C")
            await asyncio.sleep(0.08)
            pipe.send_text(" \x1b[B\r")
            result = await asyncio.wait_for(task, 2)
        self.assertEqual(result, [1])
        self.assertEqual(attempts, 2)
