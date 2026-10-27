"""Cursor safety, reusable page caches, and one-page Apple catalog reads."""

import unittest
from unittest.mock import AsyncMock

from gamdl.api import AppleMusicApi
from gamdl.pagination import MediaPage, PageCache


class PageCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_cursor_cache_and_stable_occurrence_positions(self):
        calls = []

        async def load(cursor):
            calls.append(cursor)
            return {
                None: MediaPage(("same", "same"), "after-2"),
                "after-2": MediaPage(("third",), "after-3"),
                "after-3": MediaPage(("last",)),
            }[cursor]

        pages = PageCache(load)
        self.assertEqual((await pages.get(0)).items, ("same", "same"))
        self.assertEqual(calls, [None])
        self.assertEqual((await pages.get(2)).items, ("last",))
        self.assertEqual(calls, [None, "after-2", "after-3"])
        self.assertIsNone(await pages.get(3))
        self.assertEqual(
            [item async for item in pages.iter_items()],
            [(0, "same"), (1, "same"), (2, "third"), (3, "last")],
        )
        self.assertEqual(calls, [None, "after-2", "after-3"])

    async def test_repeated_cursor_is_rejected(self):
        async def load(cursor):
            return MediaPage(("item",), "first" if cursor is None else "first")

        pages = PageCache(load)
        await pages.get(0)
        with self.assertRaisesRegex(ValueError, "Repeated pagination cursor"):
            await pages.get(1)

    async def test_failure_does_not_mark_page_as_loaded(self):
        attempts = 0

        async def load(cursor):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("transient failure")
            return MediaPage(("ok",))

        pages = PageCache(load)
        with self.assertRaises(ConnectionError):
            await pages.get(0)
        self.assertEqual(len(pages.pages), 0)
        self.assertEqual((await pages.get(0)).items, ("ok",))
        self.assertEqual(attempts, 2)


class AppleArtistPageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = AppleMusicApi.__new__(AppleMusicApi)
        self.api.storefront = "br"
        self.api._get = AsyncMock()
        self.api.get_extended_api_data = AsyncMock()

    async def test_views_return_single_page_not_entire_collection(self):
        self.api._get.return_value = {
            "data": [{"id": "1"}],
            "next": "/v1/catalog/br/artists/123/view/full-albums?offset=25",
        }
        page = await self.api.get_artist_view_page("123", "full-albums")
        self.assertEqual(page.items, ({"id": "1"},))
        self.assertIsNotNone(page.next_cursor)
        self.api._get.assert_awaited_once_with(
            "/v1/catalog/br/artists/123/view/full-albums",
            operation="get_artist_view_page",
            params={"limit": 25},
        )
        self.api.get_extended_api_data.assert_not_awaited()
        self.api.get_extended_api_data.return_value = {"data": [{"id": "2"}]}
        second = await self.api.get_artist_view_page(
            "123", "full-albums", cursor=page.next_cursor
        )
        self.assertEqual(second.items, ({"id": "2"},))
        self.api.get_extended_api_data.assert_awaited_once_with(page.next_cursor)

    async def test_relationship_endpoint_and_invalid_relationship(self):
        self.api._get.return_value = {"data": [{"id": "video1"}]}
        page = await self.api.get_artist_relationship_page(
            "123", "music-videos", limit=10
        )
        self.assertEqual(page.items, ({"id": "video1"},))
        self.api._get.assert_awaited_once_with(
            "/v1/catalog/br/artists/123/music-videos",
            operation="get_artist_relationship_page",
            params={"limit": 10},
        )
        with self.assertRaises(ValueError):
            await self.api.get_artist_relationship_page("123", "../songs")

    async def test_untrusted_pagination_urls_rejected(self):
        self.api._get.return_value = {"data": [], "next": "https://evil.example/steal"}
        with self.assertRaises(Exception):
            await self.api.get_artist_view_page("123", "full-albums")
        self.api._get.return_value = {"data": "not a list"}
        with self.assertRaisesRegex(ValueError, "pagination data"):
            await self.api.get_artist_view_page("123", "full-albums")
