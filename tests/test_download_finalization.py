"""Offline finalization tests: atomic publication, index and playlist order."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from gamdl.download import (
    DownloadResult,
    MediaDatabase,
    PlaylistWriter,
)
from gamdl.download.publication import publish_bytes
from gamdl.media import MediaSource, Song, SongCodec
from unittest.mock import Mock


class PublicationAndFinalizationStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_atomic_publisher_preserves_existing_and_cleans_staging(self):
        output = self.directory / "nested" / "song.m4a"
        self.assertTrue(publish_bytes(b"first", output, overwrite="skip"))
        self.assertFalse(publish_bytes(b"second", output, overwrite="skip"))
        self.assertEqual(output.read_bytes(), b"first")
        with self.assertRaises(FileExistsError):
            publish_bytes(b"third", output, overwrite="error")
        self.assertEqual(output.read_bytes(), b"first")
        self.assertTrue(publish_bytes(b"replaced", output, overwrite="replace"))
        self.assertEqual(output.read_bytes(), b"replaced")
        self.assertEqual(list(output.parent.glob("*.part")), [])
        with self.assertRaises(ValueError):
            publish_bytes(b"invalid", output, overwrite="bad")

    def test_playlist_relative_paths_order_and_duplicate_occurrences(self):
        playlist = PlaylistWriter(self.directory / "list" / "tracks.m3u8")
        a = self.directory / "music" / "song-a.m4a"
        b = self.directory / "music" / "song-b.m4a"
        self.assertTrue(playlist.add(a, position=2))
        self.assertTrue(playlist.add(b, position=0))
        self.assertTrue(playlist.add(a, position=1))
        self.assertFalse(playlist.add(a, position=1))
        self.assertFalse(playlist.add(a))
        self.assertTrue(playlist.add(self.directory / "music" / "song-c.m4a"))
        self.assertEqual(
            playlist.path.read_text(encoding="utf-8"),
            "#EXTM3U\n../music/song-b.m4a\n../music/song-a.m4a\n"
            "../music/song-a.m4a\n../music/song-c.m4a\n",
        )
        self.assertFalse(list(playlist.path.parent.glob("*.part")))

    def test_playlist_rejects_invalid_index_or_unrecognized_format(self):
        playlist = PlaylistWriter(self.directory / "legacy.m3u8")
        for bad in (-1, True, 0.5, "3"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                playlist.add(self.directory / "song.m4a", position=bad)
        playlist.path.write_text("not an m3u header\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            playlist.add(self.directory / "song.m4a")
        self.assertEqual(playlist.path.read_text(), "not an m3u header\n")

    def test_database_index_upsert_and_skip_preserves_codec(self):
        db = MediaDatabase(self.directory / "index" / "media.sqlite3")
        song = Song.from_id("1228739609", api=Mock())
        song.source = MediaSource("playlist_tracks", "playlist-01", 2)
        output = self.directory / "New Rules.m4a"
        output.write_bytes(b"test bytes")
        self.assertIsNone(db.get(output))
        db.record(
            song,
            DownloadResult(
                song.id, output, "downloaded", len(b"test bytes"), SongCodec.AAC_WEB
            ),
        )
        row = db.get(output)
        assert row is not None
        self.assertEqual(row["media_id"], "1228739609")
        self.assertEqual(row["media_type"], "songs")
        self.assertEqual(row["file_size"], len(b"test bytes"))
        self.assertEqual(row["codec"], "aac-web")
        self.assertEqual(row["source_kind"], "playlist_tracks")
        self.assertEqual(row["source_index"], 2)
        # A previously downloaded file may be registered again on skip.
        db.record(song, DownloadResult(song.id, output, "skipped", 0, None))
        self.assertEqual(db.get(output)["codec"], "aac-web")
        with closing(sqlite3.connect(db.path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM media_files").fetchone()[0], 1
            )

        # Replacing the same filename with a different media ID must NOT
        # falsely preserve the old media's codec in the database.
        another = Song.from_id("other-id", api=Mock())
        db.record(another, DownloadResult(another.id, output, "skipped", 0, None))
        self.assertEqual(db.get(output)["media_id"], "other-id")
        self.assertIsNone(db.get(output)["codec"])
