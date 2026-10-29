"""3.x-compatible Apple Music URL patterns and 4.x resolver routing."""

import unittest
from unittest.mock import Mock, patch

from gamdl.media import (
    AppleMusicUrlInfo,
    MediaResolver,
    MusicVideo,
    Song,
    UnsupportedMediaError,
    UploadedVideo,
)


class UrlInfoTests(unittest.TestCase):
    def test_classical_and_catalog_urls(self):
        for url, kind, media_id, slug in (
            (
                "https://music.apple.com/br/song/title/1228739609",
                "song",
                "1228739609",
                "title",
            ),
            (
                "https://classical.music.apple.com/us/album/12345",
                "album",
                "12345",
                None,
            ),
            (
                "https://music.apple.com/us/artist/artist-name/669771",
                "artist",
                "669771",
                "artist-name",
            ),
            (
                "https://music.apple.com/br/music-video/title/299199411",
                "music-video",
                "299199411",
                "title",
            ),
            (
                "https://music.apple.com/us/post/video/123456",
                "uploaded-video",
                "123456",
                "video",
            ),
            (
                "https://music.apple.com/us/uploaded-video/video/123456",
                "uploaded-video",
                "123456",
                "video",
            ),
            (
                "https://music.apple.com/us/playlist/test/pl." + "a" * 32,
                "playlist",
                "pl." + "a" * 32,
                "test",
            ),
            (
                "https://music.apple.com/us/playlist/test/pl.u-MyPlaylist01",
                "playlist",
                "pl.u-MyPlaylist01",
                "test",
            ),
        ):
            with self.subTest(url=url):
                info = AppleMusicUrlInfo.parse(url)
                self.assertEqual(info.kind, kind)
                self.assertEqual(info.media_id, media_id)
                self.assertEqual(info.slug, slug)
                self.assertFalse(info.is_library)

    def test_library_resources_and_root(self):
        for url, kind, media_id, storefront in (
            ("https://music.apple.com/library", "library", None, None),
            ("https://music.apple.com/br/library", "library", None, "br"),
            (
                "https://music.apple.com/library/playlist/p.ABC123",
                "playlist",
                "p.ABC123",
                None,
            ),
            (
                "https://music.apple.com/us/library/albums/l.123ABC",
                "album",
                "l.123ABC",
                "us",
            ),
            (
                "https://music.apple.com/library/songs/i.123ABC",
                "song",
                "i.123ABC",
                None,
            ),
            (
                "https://music.apple.com/br/library/music-videos/i.123ABC",
                "music-video",
                "i.123ABC",
                "br",
            ),
        ):
            with self.subTest(url=url):
                info = AppleMusicUrlInfo.parse(url)
                self.assertTrue(info.is_library)
                self.assertEqual(info.kind, kind)
                self.assertEqual(info.media_id, media_id)
                self.assertEqual(info.library_storefront, storefront)

    def test_sub_id_query_with_tracking_params_and_fragments(self):
        url = "https://music.apple.com/br/album/album/123?uo=4&i=456&at=123#section"
        info = AppleMusicUrlInfo.parse(url)
        self.assertEqual(info.storefront, "br")
        self.assertEqual(info.id, "123")
        self.assertEqual(info.sub_id, "456")
        self.assertEqual(info.kind, "album")
        with self.assertRaises(UnsupportedMediaError):
            AppleMusicUrlInfo.parse("https://music.apple.com/us/album/name/123?i=bad")

    def test_rejects_malformed_non_apple_or_incomplete_urls(self):
        for url in (
            "http://music.apple.com/us/song/name/123",
            "https://example.com/us/song/name/123",
            "https://music.apple.com.evil.test/us/song/name/123",
            "https://music.apple.com:443/us/song/name/123",
            "https://music.apple.com/us/song/name/not-an-id",
            "https://music.apple.com/us/album/name/123/extra",
            "https://music.apple.com/us/album/name/123?i=456/evil",
            "https://music.apple.com/song/name/123",  # Missing catalog storefront
            "https://music.apple.com/us/genre/name/123",
            "https://music.apple.com/us/album/",
            "https://music.apple.com/library/playlist/pl.invalid",
            "https://music.apple.com/library/unknown/p.123",
            "https://music.apple.com/",
            "",
        ):
            with self.subTest(url=url), self.assertRaises(UnsupportedMediaError):
                AppleMusicUrlInfo.parse(url)


class UrlResolverRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = Mock()
        self.api.iter_album_tracks = Mock(side_effect=self.album_tracks)
        self.api.iter_playlist_tracks = Mock(side_effect=self.playlist_tracks)
        self.resolver = MediaResolver(self.api)
        self.calls = []

    async def album_tracks(self, media_id, *, is_library=False):
        self.calls.append(("album", media_id, is_library))
        yield {
            "type": "library-songs" if is_library else "songs",
            "id": "i.123" if is_library else "123",
            "attributes": {"name": "Example"},
        }

    async def playlist_tracks(self, media_id, *, is_library=False):
        self.calls.append(("playlist", media_id, is_library))
        yield {
            "type": "library-songs" if is_library else "songs",
            "id": "i.123" if is_library else "123",
            "attributes": {"name": "Example"},
        }

    async def test_specific_library_album_and_playlist(self):
        for url, expected in (
            ("https://music.apple.com/library/albums/l.987", ("album", "l.987", True)),
            (
                "https://music.apple.com/library/playlist/p.ABC",
                ("playlist", "p.ABC", True),
            ),
        ):
            with self.subTest(url=url):
                media = [m async for m in self.resolver.from_url(url)]
                self.assertEqual(self.calls[-1], expected)
                self.assertEqual(len(media), 1)
                self.assertIsInstance(media[0], Song)
                self.assertTrue(media[0].is_library)

    async def test_library_song_video_and_uploaded_post(self):
        media = [
            m
            async for m in self.resolver.from_url(
                "https://music.apple.com/library/songs/i.123"
            )
        ]
        self.assertIsInstance(media[0], Song)
        self.assertEqual(media[0].id, "i.123")
        self.assertTrue(media[0].is_library)

        media = [
            m
            async for m in self.resolver.from_url(
                "https://music.apple.com/library/music-videos/i.567"
            )
        ]
        self.assertIsInstance(media[0], MusicVideo)
        self.assertEqual(media[0].id, "i.567")
        self.assertTrue(media[0].is_library)

        media = [
            m
            async for m in self.resolver.from_url(
                "https://music.apple.com/us/post/name/112"
            )
        ]
        self.assertIsInstance(media[0], UploadedVideo)
        self.assertEqual(media[0].id, "112")

    async def test_album_i_parameter_routes_only_one_song(self):
        url = "https://classical.music.apple.com/br/album/test/123?uo=4&i=999"
        media = [m async for m in self.resolver.from_url(url)]
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].id, "999")
        self.api.iter_album_tracks.assert_not_called()

    async def test_from_url_accepts_parsed_info_without_parsing_again(self):
        for url, expected_id, library in (
            ("https://music.apple.com/br/song/name/123", "123", False),
            ("https://music.apple.com/library/songs/i.456", "i.456", True),
            ("https://music.apple.com/br/album/name/789?i=987", "987", False),
        ):
            with self.subTest(url=url):
                parsed = AppleMusicUrlInfo.parse(url)
                with patch.object(
                    AppleMusicUrlInfo,
                    "parse",
                    side_effect=AssertionError("Already parsed URL was parsed again"),
                ):
                    media = [m async for m in self.resolver.from_url(parsed)]
                self.assertEqual(len(media), 1)
                self.assertEqual(media[0].id, expected_id)
                self.assertEqual(media[0].is_library, library)

    async def test_invalid_url_is_rejected_before_any_api_call(self):
        with self.assertRaises(UnsupportedMediaError):
            _ = [
                m
                async for m in self.resolver.from_url(
                    "https://music.apple.com/br/song/name/123/456"
                )
            ]
        self.api.iter_album_tracks.assert_not_called()
        self.api.iter_playlist_tracks.assert_not_called()
