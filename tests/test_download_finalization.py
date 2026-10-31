"""Offline finalization tests: atomic publication, index and playlist order."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

from gamdl.download import (
    DatabaseStatus,
    DownloadResult,
    FileStatus,
    MediaDatabase,
    OverwritePolicy,
    PlaylistTags,
    PlaylistWriter,
)
from gamdl.download.publication import publish_bytes, publish_bytes_result
from gamdl.media import MediaSource, Song, SongCodec


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

    def test_detailed_publisher_status_and_enum_policies(self):
        dest = self.directory / "result.m4a"
        self.assertEqual(
            publish_bytes_result(b"one", dest, overwrite=OverwritePolicy.SKIP),
            FileStatus.CREATED,
        )
        self.assertEqual(
            publish_bytes_result(b"two", dest, overwrite=OverwritePolicy.SKIP),
            FileStatus.SKIPPED,
        )
        self.assertEqual(
            publish_bytes_result(b"three", dest, overwrite=OverwritePolicy.REPLACE),
            FileStatus.OVERWRITTEN,
        )
        self.assertEqual(dest.read_bytes(), b"three")

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

    def test_playlist_metadata_headers_preserved_and_updated(self):
        playlist = PlaylistWriter(
            self.directory / "list.m3u8",
            tags=PlaylistTags(
                title="Old\n#INJECTED", artist="A\rB", playlist_id="p.123"
            ),
        )
        media = self.directory / "a.m4a"
        self.assertTrue(playlist.add(media, position=1))
        self.assertEqual(
            playlist.path.read_text().splitlines(),
            [
                "#EXTM3U",
                "#PLAYLIST:Old #INJECTED",
                "#Gamdl-PLAYLIST-ARTIST:A B",
                "#Gamdl-APPLE-MUSIC-ID:p.123",
                "",
                "a.m4a",
            ],
        )
        self.assertFalse(playlist.add(media, position=1))
        renamed = PlaylistWriter(playlist.path, tags=PlaylistTags(title="Updated"))
        self.assertTrue(renamed.add(media, position=1))
        self.assertEqual(playlist.path.read_text().splitlines()[1], "#PLAYLIST:Updated")
        self.assertFalse(renamed.add(media, position=1))

    def test_playlist_with_existing_comments_preserves_extinf_and_track_positions(self):
        path = self.directory / "existing.m3u8"
        path.write_text(
            "#EXTM3U\n#PLAYLIST:Original\n#CUSTOM:Keep\n"
            "#EXTINF:10,First song\nfirst.m4a\n\n",
            encoding="utf-8",
        )
        writer = PlaylistWriter(path)
        self.assertTrue(writer.add(self.directory / "second.m4a", position=1))
        self.assertEqual(
            path.read_text(),
            "#EXTM3U\n#PLAYLIST:Original\n#CUSTOM:Keep\n"
            "#EXTINF:10,First song\nfirst.m4a\nsecond.m4a\n",
        )
        self.assertFalse(writer.add(self.directory / "second.m4a", position=1))

    def test_legacy_headerless_playlists_remain_headerless_without_metadata(self):
        path = self.directory / "legacy.m3u8"
        path.write_text("#EXTM3U\nold.m4a\n")
        self.assertTrue(PlaylistWriter(path).add(self.directory / "new.m4a"))
        self.assertEqual(path.read_text(), "#EXTM3U\nold.m4a\nnew.m4a\n")

    def test_legacy_uppercase_metadata_is_read_and_rebranded_on_write(self):
        from gamdl.download.playlist import playlist_tags_from_header, split_playlist

        path = self.directory / "old-metadata.m3u8"
        path.write_text(
            "#EXTM3U\n#PLAYLIST:Favorites\n"
            "#GAMDL-PLAYLIST-ARTIST:DJ\n"
            "#GAMDL-APPLE-MUSIC-ID:p.123\n"
            "a.m4a\n",
            encoding="utf-8",
        )
        header, _ = split_playlist(path.read_text().splitlines())
        self.assertEqual(
            playlist_tags_from_header(header),
            PlaylistTags(title="Favorites", artist="DJ", playlist_id="p.123"),
        )
        writer = PlaylistWriter(path)
        self.assertTrue(writer.add(self.directory / "a.m4a", position=0))
        self.assertEqual(
            path.read_text().splitlines(),
            [
                "#EXTM3U",
                "#PLAYLIST:Favorites",
                "#Gamdl-PLAYLIST-ARTIST:DJ",
                "#Gamdl-APPLE-MUSIC-ID:p.123",
                "a.m4a",
            ],
        )
        self.assertFalse(writer.add(self.directory / "a.m4a", position=0))

    def test_database_index_upsert_and_skip_preserves_codec(self):
        db = MediaDatabase(self.directory / "index" / "media.sqlite3")
        song = Song.from_id("1228739609", api=Mock())
        song.source = MediaSource("playlist_tracks", "playlist-01", 2)
        output = self.directory / "New Rules.m4a"
        output.write_bytes(b"test bytes")
        self.assertIsNone(db.get(output))
        self.assertEqual(
            db.record(
                song,
                DownloadResult(
                    song.id, output, "downloaded", len(b"test bytes"), SongCodec.AAC_WEB
                ),
            ),
            DatabaseStatus.INSERTED,
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
        self.assertEqual(
            db.record(song, DownloadResult(song.id, output, "skipped", 0, None)),
            DatabaseStatus.UNCHANGED,
        )
        self.assertEqual(db.get(output)["codec"], "aac-web")
        with closing(sqlite3.connect(db.path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM media_files").fetchone()[0], 1
            )

        # Replacing the same filename with a different media ID must NOT
        # falsely preserve the old media's codec in the database.
        another = Song.from_id("other-id", api=Mock())
        self.assertEqual(
            db.record(another, DownloadResult(another.id, output, "skipped", 0, None)),
            DatabaseStatus.UPDATED,
        )
        self.assertEqual(db.get(output)["media_id"], "other-id")
        self.assertIsNone(db.get(output)["codec"])
