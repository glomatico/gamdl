"""Real Apple Music smoke tests; never part of the default offline test suite.

Run deliberately with:
    GAMDL_RUN_LIVE=1 uv run python -m unittest discover -s tests/live -v

Authentication settings are read from the gitignored project-root .env file.
These tests are read-only, perform no downloads or DRM license exchanges,
and never print credential values or raw account responses.
"""

from __future__ import annotations

import asyncio
import os
from time import monotonic
import unittest

from gamdl.api import ItunesApi
from tests.live_auth import LiveSettings, open_live_session

ALBUM_ID = "1559885420"  # Whenever You Need Somebody, original album (BR)
SONG_ID = "1559885421"  # Never Gonna Give You Up, from that album
MUSIC_VIDEO_ID = "299199411"  # Never Gonna Give You Up music video
_next_test_start = 0.0


@unittest.skipUnless(
    os.environ.get("GAMDL_RUN_LIVE") == "1",
    "Opt-in only: set GAMDL_RUN_LIVE=1 to contact Apple Music",
)
class AppleMusicLiveTests(unittest.IsolatedAsyncioTestCase):
    """A small, read-only Rick Astley fixture set with conservative pacing."""

    async def asyncSetUp(self) -> None:
        global _next_test_start
        # IsolatedAsyncioTestCase uses a new loop for each test; this module
        # level clock adds a modest delay across separate sessions as well.
        delay = _next_test_start - monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        _next_test_start = monotonic() + 1.5
        self._session_context = open_live_session(LiveSettings.from_env())
        self.session = await self._session_context.__aenter__()
        self.addAsyncCleanup(self._session_context.__aexit__, None, None, None)
        self.api = self.session.api

    async def test_authentication(self) -> None:
        """An available wrapper takes precedence over other sources."""
        self.assertIn(self.session.source, {"wrapper", "media_user_token", "cookies"})
        expected_source = os.environ.get("GAMDL_EXPECT_AUTH_SOURCE")
        if expected_source:
            self.assertEqual(self.session.source, expected_source)
        self.assertEqual(self.api.storefront, "br")
        self.assertIsInstance(self.api.account_info, dict)

    async def test_album(self) -> None:
        album = (await self.api.get_album(ALBUM_ID))["data"][0]
        self.assertEqual(album["id"], ALBUM_ID)
        self.assertEqual(album["type"], "albums")
        self.assertIn("Whenever You Need Somebody", album["attributes"]["name"])

    async def test_song(self) -> None:
        song = (await self.api.get_song(SONG_ID))["data"][0]
        self.assertEqual(song["id"], SONG_ID)
        self.assertEqual(song["type"], "songs")
        self.assertIn("Never Gonna Give You Up", song["attributes"]["name"])

    async def test_music_video(self) -> None:
        video = (await self.api.get_music_video(MUSIC_VIDEO_ID))["data"][0]
        self.assertEqual(video["id"], MUSIC_VIDEO_ID)
        self.assertEqual(video["type"], "music-videos")
        self.assertIn("Never Gonna Give You Up", video["attributes"]["name"])

    async def test_album_tracks_iterator(self) -> None:
        tracks = [
            track["id"] async for track in self.api.iter_album_tracks(ALBUM_ID, limit=3)
        ]
        self.assertGreaterEqual(len(tracks), 8)
        self.assertEqual(tracks[0], SONG_ID)
        self.assertEqual(len(tracks), len(set(tracks)))

    async def test_catalog_search_iterator(self) -> None:
        # The iterator calls get_search_results internally. One live search is
        # sufficient here; separate mocked tests exercise full pagination.
        iterator = self.api.iter_search_results(
            "Rick Astley Never Gonna Give You Up", types="songs,music-videos", limit=3
        )
        received = False
        async for song in iterator:
            self.assertEqual(song["type"], "songs")
            received = True
            break  # Verify lazy consumption without exhausting search pages.
        self.assertTrue(received)

    async def test_standard_lyrics(self) -> None:
        lyrics = await self.api.get_lyrics(SONG_ID)
        resources = lyrics.get("data") or []
        self.assertTrue(resources)
        ttml = resources[0].get("attributes", {}).get("ttml")
        self.assertIsInstance(ttml, str)
        self.assertIn("<tt", ttml)

    async def test_syllable_lyrics(self) -> None:
        lyrics = await self.api.get_syllable_lyrics(SONG_ID)
        resources = lyrics.get("data") or []
        self.assertTrue(resources)
        ttml = resources[0].get("attributes", {}).get("ttml")
        self.assertIsInstance(ttml, str)
        self.assertIn("<tt", ttml)

    async def test_library_page_is_readable(self) -> None:
        page = await self.api.get_library_playlists(limit=2)
        self.assertIsInstance(page.get("data"), list)

    async def test_itunes_album_lookup(self) -> None:
        async with await ItunesApi.create(storefront="br") as itunes:
            self.assertEqual(itunes.storefront_id, 143503)
            response = await itunes.get_lookup_result(ALBUM_ID)
        self.assertGreater(response["resultCount"], 0)
        self.assertTrue(
            any(
                str(item.get("collectionId")) == ALBUM_ID
                for item in response["results"]
            )
        )


if __name__ == "__main__":
    unittest.main()
