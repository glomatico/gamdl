"""Offline finalization tests: atomic publication, index and playlist order."""

from __future__ import annotations

import errno
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from gamdl.download import (
    DatabaseStatus,
    DownloadResult,
    FileStatus,
    MediaDatabase,
    OverwritePolicy,
    PlaylistTags,
    PlaylistWriter,
)
from gamdl.download.publication import publish_bytes, publish_bytes_result, publish_file
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

    def test_maximum_length_sidecar_is_published_and_replaced_atomically(self):
        """Do not derive temporary filenames from a 255-byte media basename."""
        media = self.directory / ("A" * 251 + ".m4a")
        media.write_bytes(b"already downloaded")
        lyrics = media.with_suffix(".lrc")
        self.assertEqual(len(lyrics.name.encode("utf-8")), 255)
        self.assertEqual(
            publish_bytes_result(b"[00:00.00] First", lyrics, overwrite="skip"),
            FileStatus.CREATED,
        )
        self.assertEqual(
            publish_bytes_result(b"ignored", lyrics, overwrite="skip"),
            FileStatus.SKIPPED,
        )
        with self.assertRaises(FileExistsError):
            publish_bytes_result(b"ignored", lyrics, overwrite="error")
        self.assertEqual(
            publish_bytes_result(b"[00:00.00] Updated", lyrics, overwrite="replace"),
            FileStatus.OVERWRITTEN,
        )
        self.assertEqual(lyrics.read_bytes(), b"[00:00.00] Updated")
        self.assertEqual(media.read_bytes(), b"already downloaded")
        self.assertEqual(list(self.directory.glob(".gamdl-*")), [])

    def test_maximum_length_media_publication_replace_and_cross_device(self):
        """Both media publication branches must work with NAME_MAX filenames."""
        source = self.directory / "staged.bin"
        source.write_bytes(b"new media")
        target = self.directory / ("W" * 251 + ".m4a")
        target.write_bytes(b"old media")
        self.assertTrue(publish_file(source, target, overwrite="replace"))
        self.assertEqual(target.read_bytes(), b"new media")

        target.unlink()
        original_link = os.link
        attempted = False

        def cross_device_once(source_path, target_path):
            nonlocal attempted
            if not attempted:
                attempted = True
                raise OSError(errno.EXDEV, "different filesystem")
            return original_link(source_path, target_path)

        with patch(
            "gamdl.download.publication.os.link",
            side_effect=cross_device_once,
        ):
            self.assertTrue(publish_file(source, target, overwrite="skip"))
        self.assertTrue(attempted)
        self.assertEqual(target.read_bytes(), b"new media")
        self.assertEqual(list(self.directory.glob(".gamdl-*")), [])

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
