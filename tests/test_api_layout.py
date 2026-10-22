"""Keep Apple Music's public API stable across endpoint package refactors."""

import inspect
import unittest

from gamdl.api import AppleMusicApi
from gamdl.api.apple_music import AppleMusicApi as PackageAppleMusicApi
from gamdl.api.apple_music.client import AppleMusicApi as ClientAppleMusicApi
from gamdl.api.apple_music.catalog import CatalogEndpoints
from gamdl.api.apple_music.drm import DrmEndpoints
from gamdl.api.apple_music.library import LibraryEndpoints
from gamdl.api.apple_music.playback import PlaybackEndpoints
from gamdl.api.apple_music.search import SearchEndpoints


class AppleMusicPackageTests(unittest.TestCase):
    def test_public_imports_refer_to_the_same_client_class(self):
        self.assertIs(AppleMusicApi, PackageAppleMusicApi)
        self.assertIs(AppleMusicApi, ClientAppleMusicApi)

    def test_endpoint_groups_are_part_of_the_public_client(self):
        expected = {
            CatalogEndpoints: (
                "get_song",
                "get_lyrics",
                "get_syllable_lyrics",
                "get_music_video",
                "get_uploaded_video",
                "get_album",
                "get_playlist",
                "get_artist",
                "iter_playlist_tracks",
                "iter_album_tracks",
                "iter_artist_view",
            ),
            LibraryEndpoints: (
                "get_library_song",
                "get_library_music_video",
                "get_library_album",
                "get_library_playlist",
                "get_library_songs",
                "get_library_music_videos",
                "get_library_albums",
                "get_library_playlists",
                "iter_library_playlists",
                "iter_library_songs",
                "iter_library_music_videos",
                "iter_library_albums",
            ),
            SearchEndpoints: ("get_search_results", "iter_search_results"),
            PlaybackEndpoints: ("get_assets", "get_webplayback"),
            DrmEndpoints: ("get_license_exchange",),
        }
        for group, methods in expected.items():
            self.assertTrue(issubclass(AppleMusicApi, group))
            for name in methods:
                with self.subTest(group=group.__name__, name=name):
                    self.assertIs(getattr(AppleMusicApi, name), group.__dict__[name])

    def test_lazy_iterators_remain_coroutines_or_async_generators(self):
        for name in (
            "iter_library_songs",
            "iter_library_playlists",
            "iter_library_music_videos",
            "iter_library_albums",
            "iter_playlist_tracks",
            "iter_album_tracks",
            "iter_artist_view",
            "iter_search_results",
        ):
            with self.subTest(name=name):
                self.assertTrue(
                    inspect.isasyncgenfunction(getattr(AppleMusicApi, name))
                )

    def test_constructor_and_playback_defaults_remain_unchanged(self):
        self.assertEqual(
            inspect.signature(AppleMusicApi.get_assets)
            .parameters["hls_profile"]
            .default,
            "enhancedHls",
        )
        self.assertEqual(
            inspect.signature(AppleMusicApi.get_webplayback)
            .parameters["is_library"]
            .default,
            False,
        )
        self.assertEqual(
            inspect.signature(AppleMusicApi.get_license_exchange)
            .parameters["key_system"]
            .default,
            "com.widevine.alpha",
        )


if __name__ == "__main__":
    unittest.main()
