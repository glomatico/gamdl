"""Search public editorial and user-shared playlists without library lookups."""

import unittest
from unittest.mock import AsyncMock, Mock

from gamdl.media import AppleMusicUrlInfo
from gamdl.search import create_search_caches


def playlist(media_id: str, playlist_type: str) -> dict:
    return {
        "id": media_id,
        "type": "playlists",
        "attributes": {
            "name": media_id,
            "curatorName": "A curator",
            "playlistType": playlist_type,
        },
    }


class PublicPlaylistSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_editorial_and_shared_playlist_categories_share_catalog_query(self):
        api = Mock()
        api.get_search_results = AsyncMock(
            return_value={
                "results": {
                    "playlists": {
                        "data": [
                            playlist("pl.editorial", "editorial"),
                            playlist("pl.brand", "external"),
                            playlist("pl.user", "user-shared"),
                            playlist("pl.mix", "personal-mix"),
                        ]
                    }
                }
            }
        )
        api.get_extended_api_data = AsyncMock()
        caches = create_search_caches(api, "playlist", ("playlists", "user-playlists"))

        editorial = await caches["playlists"].get(0)
        shared = await caches["user-playlists"].get(0)
        self.assertEqual(
            [item["id"] for item in editorial.items],
            ["pl.editorial", "pl.brand"],
        )
        self.assertEqual([item["id"] for item in shared.items], ["pl.user"])
        self.assertEqual(shared.items[0]["type"], "playlists")
        api.get_search_results.assert_awaited_once_with(
            "playlist", types="playlists", limit=50
        )
        api.get_extended_api_data.assert_not_awaited()

    async def test_shared_category_skips_curated_only_pages(self):
        api = Mock()
        api.get_search_results = AsyncMock(
            return_value={
                "results": {
                    "playlists": {
                        "data": [playlist("pl.editorial", "editorial")],
                        "next": "page-two",
                    }
                }
            }
        )
        api.get_extended_api_data = AsyncMock(
            return_value={
                "results": {
                    "playlists": {
                        "data": [playlist("pl.user", "user-shared")],
                    }
                }
            }
        )
        caches = create_search_caches(api, "test", ("user-playlists", "playlists"))
        self.assertEqual(
            [item["id"] for item in (await caches["user-playlists"].get(0)).items],
            ["pl.user"],
        )
        self.assertEqual(
            [item["id"] for item in (await caches["playlists"].get(0)).items],
            ["pl.editorial"],
        )
        api.get_search_results.assert_awaited_once()
        api.get_extended_api_data.assert_awaited_once_with("page-two")

    async def test_empty_user_category_is_valid_and_does_not_fetch_library(self):
        api = Mock()
        api.get_search_results = AsyncMock(
            return_value={
                "results": {"playlists": {"data": [playlist("pl.brand", "external")]}}
            }
        )
        caches = create_search_caches(api, "test", ("user-playlists",))
        self.assertEqual((await caches["user-playlists"].get(0)).items, ())
        api.get_search_results.assert_awaited_once()

    def test_direct_uploaded_video_urls_still_supported(self):
        info = AppleMusicUrlInfo.parse(
            "https://music.apple.com/us/post/some-title/123456789"
        )
        self.assertEqual(info.kind, "uploaded-video")
