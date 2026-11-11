"""Local catalog-ID lookups must be fast and reject obsolete index entries."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from gamdl.download import MediaDatabase


class MediaLookupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = self.root / "Apple Music"
        self.library.mkdir()
        self.database = MediaDatabase(self.root / "media.sqlite3", root=self.root)

    def add(self, path: Path, media_type: str, media_id: str, codec=None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"sample")
        self.database.index_file(path, media_type, media_id)
        if codec:
            with sqlite3.connect(self.database.path) as connection:
                connection.execute(
                    "UPDATE media_files SET codec=? WHERE path=?",
                    (codec, path.relative_to(self.root).as_posix()),
                )
        return path

    def test_multiple_copies_can_be_selected_by_codec_and_extension(self):
        aac = self.add(self.library / "one.m4a", "songs", "123", "aac")
        alac = self.add(self.library / "two.m4a", "songs", "123", "alac")
        self.assertEqual(
            Path(self.database.find_by_media_id("songs", "123", codec="aac")["path"]),
            aac,
        )
        self.assertEqual(
            Path(self.database.find_by_media_id("songs", "123", codec="alac")["path"]),
            alac,
        )
        self.assertIsNone(self.database.find_by_media_id("songs", "123", codec="atmos"))
        self.assertIsNone(self.database.find_by_media_id("music-videos", "123"))
        self.assertIsNone(
            self.database.find_by_media_id("songs", "123", extensions=(".mp3",))
        )
        self.assertEqual(len(self.database.local_records(self.library)), 2)
        with sqlite3.connect(self.database.path) as connection:
            indices = [
                row[1] for row in connection.execute("PRAGMA index_list(media_files)")
            ]
        self.assertIn("media_id_lookup_idx", indices)

    def test_missing_changed_and_other_output_directory_are_ignored(self):
        old = self.add(self.library / "lost.m4a", "songs", "456")
        external = self.add(self.root / "Other" / "external.m4a", "songs", "456")
        self.assertIn(
            Path(self.database.find_by_media_id("songs", "456")["path"]),
            (old, external),
        )
        self.assertIsNotNone(
            self.database.find_by_media_id("songs", "456", root=self.library)
        )
        old.write_bytes(b"not the original bytes")
        self.assertIsNone(
            self.database.find_by_media_id("songs", "456", root=self.library)
        )
        self.assertEqual(
            Path(self.database.find_by_media_id("songs", "456")["path"]),
            external,
        )
        old.unlink()
        self.assertIsNone(
            self.database.find_by_media_id("songs", "456", root=self.library)
        )

    def test_unknown_codec_requires_provenance_for_exact_filter(self):
        path = self.add(self.library / "track.m4a", "songs", "101")
        self.assertEqual(
            Path(self.database.find_by_media_id("songs", "101")["path"]), path
        )
        self.assertIsNone(self.database.find_by_media_id("songs", "101", codec="alac"))

    def test_absent_database_does_not_create_it(self):
        self.assertIsNone(self.database.find_by_media_id("songs", "123"))
        self.assertEqual(self.database.local_records(self.library), [])
        self.assertFalse(self.database.path.exists())

    def test_symlink_is_not_valid_indexed_media(self):
        target = self.add(self.library / "target.m4a", "songs", "999")
        link = self.library / "linked.m4a"
        link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "Symlinked media"):
            self.database.index_file(link, "songs", "1000")
        self.assertIsNone(self.database.find_by_media_id("songs", "1000"))
