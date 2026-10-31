"""Offline organization with real MP4/MP3 tags and isolated libraries."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from gamdl import _native
from gamdl.cli.main import main
from gamdl.download import Tags
from gamdl.download.database import MediaDatabase
from gamdl.muxer import Muxer


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for MP4 test fixtures")
class OrganizeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.config = self.root / "config.yaml"
        self.db = MediaDatabase(self.root / "index.sqlite3")
        self.config.write_text(
            "output:\n"
            f"  directory: '{self.library}'\n"
            "database:\n  enabled: true\n"
            f"  path: '{self.db.path}'\n",
            encoding="utf-8",
        )
        self.runner = CliRunner()

    def invoke(self, *args):
        return self.runner.invoke(
            main, ["--config", str(self.config), "organize", *args]
        )

    def media(self, relative: str, tags: Tags | None = None) -> Path:
        path = self.library / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=600:duration=0.1",
                "-c:a",
                "aac" if path.suffix != ".mp3" else "libmp3lame",
                "-y",
                str(path),
            ],
            capture_output=True,
            check=True,
        )
        if tags is not None:
            Muxer().tag(path, tags=tags)
        return path

    def test_move_sidecars_playlist_index_and_dry_run(self):
        old = self.media(
            "unsorted/track.m4a",
            Tags(
                title="Song/one",
                artist="Artist",
                album_artist="Artist",
                album="Album",
                track_number=3,
                title_id=12345,
            ),
        )
        (old.with_suffix(".lrc")).write_text("Lyrics\n", encoding="utf-8")
        (old.with_suffix(".jpg")).write_bytes(b"cover")
        contents = hashlib.sha256(old.read_bytes()).digest()
        playlist = self.library / "Playlists" / "Curator" / "List.m3u8"
        playlist.parent.mkdir(parents=True)
        ref = Path(os.path.relpath(old, playlist.parent)).as_posix()
        playlist.write_text(f"#EXTM3U\n#EXTINF:1,Title\n{ref}\n{ref}\n")
        self.db.index_file(old, "songs", "12345")
        with sqlite3.connect(self.db.path) as db:
            db.execute(
                "UPDATE media_files SET codec = 'aac-web', source_kind = 'album-tracks' "
                "WHERE path = ?",
                (str(old),),
            )

        expected = self.library / "Artist" / "Album" / "03 Song_one.m4a"
        before = playlist.read_bytes()
        dry = self.invoke("--dry-run")
        self.assertEqual(dry.exit_code, 0, dry.output)
        self.assertIn("would move 1", dry.output)
        self.assertTrue(old.is_file())
        self.assertFalse(expected.exists())
        self.assertEqual(playlist.read_bytes(), before)
        self.assertIsNone(self.db.get(expected))

        moved = self.invoke()
        self.assertEqual(moved.exit_code, 0, moved.output)
        self.assertIn("moved 1", moved.output)
        self.assertTrue(expected.is_file())
        self.assertFalse(old.exists())
        self.assertEqual(hashlib.sha256(expected.read_bytes()).digest(), contents)
        self.assertEqual(expected.with_suffix(".lrc").read_text(), "Lyrics\n")
        self.assertEqual(expected.with_suffix(".jpg").read_bytes(), b"cover")
        self.assertFalse(old.parent.exists())
        new_ref = Path(os.path.relpath(expected, playlist.parent)).as_posix()
        self.assertEqual(
            playlist.read_text(),
            f"#EXTM3U\n#EXTINF:1,Title\n{new_ref}\n{new_ref}\n",
        )
        self.assertIsNone(self.db.get(old))
        record = self.db.get(expected)
        self.assertEqual(record["media_id"], "12345")
        self.assertEqual(record["codec"], "aac-web")
        self.assertEqual(record["source_kind"], "album-tracks")
        second = self.invoke()
        self.assertEqual(second.exit_code, 0, second.output)
        self.assertIn("moved 0", second.output)

    def test_collision_preserves_both_files_and_occupied_name(self):
        tags = Tags(
            title="Same",
            artist="Artist",
            album_artist="Artist",
            album="Album",
            track_number=1,
        )
        first = self.media("a/first.m4a", tags)
        second = self.media("b/second.m4a", tags)
        original = first.read_bytes()
        target = self.library / "Artist" / "Album" / "01 Same.m4a"
        dry = self.invoke("--dry-run")
        self.assertEqual(dry.exit_code, 1, dry.output)
        self.assertIn("collisions 2", dry.output)
        result = self.invoke()
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(first.read_bytes(), original)
        self.assertFalse(target.exists())

        second.unlink()
        target.parent.mkdir(parents=True)
        target.write_bytes(b"existing content")
        occupied = self.invoke()
        self.assertEqual(occupied.exit_code, 1, occupied.output)
        self.assertEqual(target.read_bytes(), b"existing content")
        self.assertTrue(first.exists())

    def test_explicit_directory_override_templates_and_mp3(self):
        custom = self.root / "custom"
        custom.mkdir()
        path = custom / "raw" / "record.mp3"
        # Create the fixture in a different library root.
        self.library = custom
        path = self.media("raw/record.mp3", Tags(title="T/est?", artist="Me"))
        self.assertEqual(_native.read_mp3_organize_tags(str(path))["title"], "T/est?")
        result = self.invoke(
            "--directory",
            str(custom),
            "--no-album-folder-template",
            "New/{artist}",
            "--no-album-file-template",
            "{title}!",
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue((custom / "New" / "Me" / "T_est_!.mp3").is_file())

    def test_mp4_requires_media_type_and_missing_tags_are_skipped(self):
        unknown = self.media(
            "raw/unknown.mp4",
            Tags(title="Unknown", artist="Someone"),
        )
        missing = self.media("raw/missing.m4a")
        recognized = self.media(
            "raw/recognized.mp4",
            Tags(title="Video", artist="Someone", media_type=6, title_id=987),
        )
        result = self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("ambiguous 1", result.output)
        self.assertIn("untagged 1", result.output)
        self.assertTrue(unknown.is_file())
        self.assertTrue(missing.is_file())
        target = self.library / "Someone" / "Unknown Album" / "Video.mp4"
        self.assertTrue(target.is_file())
        self.assertFalse(recognized.exists())
        self.assertEqual(self.db.get(target)["media_type"], "music-videos")

    def test_failed_sidecar_link_rolls_back_and_keeps_database(self):
        old = self.media(
            "raw/song.m4a",
            Tags(title="Song", artist="Artist", title_id=234),
        )
        old.with_suffix(".lrc").write_text("lyrics")
        self.db.index_file(old, "songs", "234")
        real_link = os.link

        def failing_link(source, target, **kwargs):
            if str(target).endswith(".lrc"):
                raise OSError("simulated sidecar failure")
            return real_link(source, target, **kwargs)

        with patch("gamdl.download.organizer.os.link", side_effect=failing_link):
            result = self.invoke()
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("simulated sidecar failure", result.output)
        self.assertTrue(old.exists())
        self.assertTrue(old.with_suffix(".lrc").exists())
        target = self.library / "Artist" / "Unknown Album" / "Song.m4a"
        self.assertFalse(target.exists())
        self.assertFalse(target.with_suffix(".lrc").exists())
        self.assertIsNotNone(self.db.get(old))
        self.assertIsNone(self.db.get(target))

    def test_symlinked_destination_directory_is_never_followed(self):
        source = self.media("raw/song.m4a", Tags(title="Song", artist="Artist"))
        elsewhere = self.root / "outside"
        elsewhere.mkdir()
        (self.library / "Artist").symlink_to(elsewhere, target_is_directory=True)
        result = self.invoke()
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("collisions 1", result.output)
        self.assertTrue(source.is_file())
        self.assertEqual(list(elsewhere.rglob("*")), [])

    def test_existing_stale_database_destination_is_not_overwritten(self):
        old = self.media(
            "raw/song.m4a", Tags(title="Song", artist="Artist", title_id=234)
        )
        expected = self.library / "Artist" / "Unknown Album" / "Song.m4a"
        expected.parent.mkdir(parents=True)
        expected.write_bytes(b"stale")
        self.db.index_file(expected, "songs", "987")
        expected.unlink()
        self.db.index_file(old, "songs", "234")
        result = self.invoke()
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertTrue(old.exists())
        self.assertFalse(expected.exists())
        self.assertEqual(self.db.get(old)["media_id"], "234")
        self.assertEqual(self.db.get(expected)["media_id"], "987")

    def test_template_override_preserves_multiple_disc_numbering(self):
        source = self.media(
            "raw/song.m4a",
            Tags(
                title="A:B",
                artist="Band",
                album_artist="Band",
                album="First",
                track_number=2,
                disc_number=3,
                disc_total=4,
            ),
        )
        preview = self.invoke(
            "--album-folder-template",
            "{album_artist}/{album} (Remastered)",
            "--multi-disc-file-template",
            "{disc}.{track:02d} {title}",
            "--dry-run",
        )
        self.assertEqual(preview.exit_code, 0, preview.output)
        expected = self.library / "Band" / "First (Remastered)" / "3.02 A_B.m4a"
        self.assertIn(expected.name, preview.output)
        self.assertTrue(source.is_file())
        moved = self.invoke(
            "--album-folder-template",
            "{album_artist}/{album} (Remastered)",
            "--multi-disc-file-template",
            "{disc}.{track:02d} {title}",
        )
        self.assertEqual(moved.exit_code, 0, moved.output)
        self.assertTrue(expected.exists())
