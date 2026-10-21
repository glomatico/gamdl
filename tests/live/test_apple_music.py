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

from gamdl.api import GamdlRateLimitError, ItunesApi, WrapperApi
from tests.live_auth import LiveSettings, open_live_session

ALBUM_ID = "1559885420"  # Whenever You Need Somebody, original album (BR)
SONG_ID = "1559885421"  # Never Gonna Give You Up, from that album
MUSIC_VIDEO_ID = "299199411"  # Never Gonna Give You Up music video
ARTIST_ID = "669771"  # Rick Astley
PLAYLIST_ID = "pl.504a9420747e43ec93e4faa999a8bef9"  # Rick Astley Essentials (BR)
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
        _next_test_start = monotonic() + 2.0
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

    async def test_song_assets(self) -> None:
        """Song assets include playable metadata but are not downloaded."""
        payload = await self.api.get_assets(SONG_ID, kind="song")
        assets = payload["results"]["assets"]
        self.assertIsInstance(assets, list)
        self.assertTrue(assets)
        self.assertTrue(any("url" in asset for asset in assets))

    async def test_music_video_assets(self) -> None:
        """Apple uses the kind 'musicVideo', not 'music-video', for assets."""
        payload = await self.api.get_assets(MUSIC_VIDEO_ID, kind="musicVideo")
        assets = payload["results"]["assets"]
        self.assertIsInstance(assets, list)
        self.assertTrue(assets)
        self.assertTrue(any("url" in asset for asset in assets))

    async def test_webplayback_song(self) -> None:
        """Playback metadata is available without requesting a DRM license."""
        payload = await self.api.get_webplayback(SONG_ID)
        self.assertIsInstance(payload.get("songList"), list)
        self.assertTrue(payload["songList"])
        self.assertEqual(str(payload["songList"][0]["songId"]), SONG_ID)

    async def test_wrapper_playback_song(self) -> None:
        """Wrapper playback returns metadata, not a downloaded or decrypted file."""
        if self.session.source != "wrapper":
            self.skipTest("Wrapper playback requires the wrapper auth source")

        settings = LiveSettings.from_env()
        async with await WrapperApi.create(base_url=settings.wrapper_url) as wrapper:
            payload = await wrapper.get_playback(SONG_ID)
        self.assertIsInstance(payload.get("songList"), list)
        self.assertTrue(payload["songList"])

    async def test_artist(self) -> None:
        artist = (await self.api.get_artist(ARTIST_ID))["data"][0]
        self.assertEqual(artist["id"], ARTIST_ID)
        self.assertEqual(artist["type"], "artists")
        self.assertIn("Rick Astley", artist["attributes"]["name"])

    async def test_artist_view_iterator(self) -> None:
        """Check the artist view endpoint while consuming only the first item."""
        found = False
        async for song in self.api.iter_artist_view(ARTIST_ID, "top-songs", limit=3):
            self.assertEqual(song["type"], "songs")
            found = True
            break
        self.assertTrue(found)

    async def test_catalog_playlist(self) -> None:
        playlist = (await self.api.get_playlist(PLAYLIST_ID))["data"][0]
        self.assertEqual(playlist["id"], PLAYLIST_ID)
        self.assertEqual(playlist["type"], "playlists")
        self.assertIn("Rick Astley", playlist["attributes"]["name"])

    async def test_catalog_playlist_tracks_iterator(self) -> None:
        """Fetch a few playlist tracks using the direct tracks relationship."""
        found = 0
        async for track in self.api.iter_playlist_tracks(PLAYLIST_ID, limit=2):
            self.assertIn(track["type"], {"songs", "music-videos"})
            found += 1
            if found == 3:
                break  # The third track verifies fetching a subsequent page.
        self.assertEqual(found, 3)

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
        # Apple frequently rate-limits search even while other endpoints work.
        # Only an explicit HTTP 429 is treated as an external-service skip.
        iterator = self.api.iter_search_results(
            "Rick Astley Never Gonna Give You Up", types="songs,music-videos", limit=3
        )
        received = False
        try:
            async for song in iterator:
                self.assertEqual(song["type"], "songs")
                received = True
                break  # Verify lazy consumption without exhausting search pages.
        except GamdlRateLimitError:
            self.skipTest("Apple Music search API returned HTTP 429")
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

    async def test_library_collections(self) -> None:
        """Validate the top-level responses without exposing library items."""
        for getter, expected_type in (
            (self.api.get_library_songs, "library-songs"),
            (self.api.get_library_albums, "library-albums"),
            (self.api.get_library_music_videos, "library-music-videos"),
        ):
            page = await getter(limit=1)
            self.assertIsInstance(page.get("data"), list)
            for resource in page["data"]:
                self.assertEqual(resource["type"], expected_type)

    async def test_library_song_detail(self) -> None:
        """Fetch one real library song, if the account has any."""
        first_page = await self.api.get_library_songs(limit=1)
        if not first_page["data"]:
            self.skipTest("No library songs available for detail test")
        song_id = first_page["data"][0]["id"]
        detail = (await self.api.get_library_song(song_id))["data"][0]
        self.assertEqual(detail["id"], song_id)
        self.assertEqual(detail["type"], "library-songs")

    async def test_library_album_detail(self) -> None:
        """Fetch one real library album, without relying on a fixed library ID."""
        first_page = await self.api.get_library_albums(limit=1)
        if not first_page["data"]:
            self.skipTest("No library albums available for detail test")
        album_id = first_page["data"][0]["id"]
        detail = (await self.api.get_library_album(album_id))["data"][0]
        self.assertEqual(detail["id"], album_id)
        self.assertEqual(detail["type"], "library-albums")

    async def test_optional_library_video_and_playlist_details(self) -> None:
        """Validate available library details without assuming the library size."""
        checks = (
            (
                self.api.get_library_music_videos,
                self.api.get_library_music_video,
                "library-music-videos",
            ),
            (
                self.api.get_library_playlists,
                self.api.get_library_playlist,
                "library-playlists",
            ),
        )
        checked = 0
        for list_items, detail, expected_type in checks:
            page = await list_items(limit=1)
            self.assertIsInstance(page.get("data"), list)
            if not page["data"]:
                continue
            item_id = page["data"][0]["id"]
            item = (await detail(item_id))["data"][0]
            self.assertEqual(item["id"], item_id)
            self.assertEqual(item["type"], expected_type)
            checked += 1
        if not checked:
            self.skipTest("No library music videos or playlists to test by ID")

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

    async def test_itunes_album_page(self) -> None:
        """Retrieve Apple's storefront-scoped album page metadata."""
        async with await ItunesApi.create(storefront="br") as itunes:
            response = await itunes.get_itunes_page("album", ALBUM_ID)
        self.assertIsInstance(response, dict)
        self.assertIn("storePlatformData", response)
        self.assertIn("pageData", response)


if __name__ == "__main__":
    unittest.main()
