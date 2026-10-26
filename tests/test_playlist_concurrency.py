"""Regression tests for simultaneous local M3U updates."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep
from unittest import TestCase
from unittest.mock import patch

from gamdl.download import PlaylistWriter
import gamdl.download.playlist as playlist_module


class PlaylistConcurrencyTests(TestCase):
    def test_writers_to_one_playlist_cannot_lose_previous_entries(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            playlist_path = root / "Playlists" / "Favorites.m3u8"
            real_publish = playlist_module.publish_bytes

            def delayed_publish(*args, **kwargs):
                # Encourage the read/modify/write interleaving which caused
                # the original lost-entry race before per-path synchronization.
                sleep(0.003)
                return real_publish(*args, **kwargs)

            def write(index):
                return PlaylistWriter(playlist_path).add(
                    root / "Songs" / f"song{index}.m4a", position=index
                )

            with patch.object(
                playlist_module, "publish_bytes", side_effect=delayed_publish
            ):
                with ThreadPoolExecutor(max_workers=12) as pool:
                    self.assertTrue(all(pool.map(write, range(20))))

            entries = playlist_path.read_text().splitlines()
            self.assertEqual(entries[0], "#EXTM3U")
            self.assertEqual(entries[1:], [f"../Songs/song{i}.m4a" for i in range(20)])
