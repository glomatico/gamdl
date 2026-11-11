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
        self.db = MediaDatabase(self.root / "index.sqlite3", root=self.library)
        self.config.write_text(
            "output:\n"
            f"  directory: '{self.library}'\n"
            "database:\n  disabled: false\n"
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
                (old.relative_to(self.library).as_posix(),),
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

    def test_playlists_only_recovers_old_album_case_and_track_number(self):
        actual = self.media(
            "Janet Jackson/All for You/18 Doesn't Really Matter.m4a",
            Tags(
                title="Doesn't Really Matter",
                artist="Janet Jackson",
                album="All for You",
            ),
        )
        # Organize would normally move this malformed filename to its album
        # template, but --playlists-only must leave every media file alone.
        expected_bytes = actual.read_bytes()
        playlist = self.library / "Playlists/DJ/Dance.m3u8"
        playlist.parent.mkdir(parents=True)
        playlist.write_text(
            "#EXTM3U\n#PLAYLIST:Dance\n#Gamdl-PLAYLIST-ARTIST:DJ\n"
            "#EXTINF:30,Janet\n../../Janet Jackson/All For You/17 Doesn't Really Matter.m4a\n"
            "../../Cyndi Lauper/True Colors/01 Change of Heart.m4a\n"
        )
        before = playlist.read_bytes()
        dry = self.invoke("--playlists-only", "--dry-run")
        self.assertEqual(dry.exit_code, 0, dry.output)
        self.assertEqual(playlist.read_bytes(), before)
        result = self.invoke("--playlists-only")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Playlist references repaired: 1; unresolved: 1", result.output)
        self.assertIn(
            "../../Janet Jackson/All for You/18 Doesn't Really Matter.m4a",
            playlist.read_text(),
        )
        self.assertIn(
            "../../Cyndi Lauper/True Colors/01 Change of Heart.m4a",
            playlist.read_text(),
        )
        self.assertEqual(actual.read_bytes(), expected_bytes)
        self.assertTrue(actual.exists())
        again = self.invoke("--playlists-only")
        self.assertEqual(again.exit_code, 0, again.output)
        self.assertIn("Playlist references repaired: 0; unresolved: 1", again.output)

    def test_playlist_repair_ignores_ambiguous_alternatives(self):
        self.media("A/Album/01 Track.m4a")
        self.media("A/Album/02 Track.m4a")
        playlist = self.library / "Playlists/Me/List.m3u8"
        playlist.parent.mkdir(parents=True)
        playlist.write_text("#EXTM3U\n../../A/Album/03 Track.m4a\n")
        result = self.invoke("--playlists-only")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Playlist references repaired: 0; unresolved: 1", result.output)
        self.assertEqual(playlist.read_text(), "#EXTM3U\n../../A/Album/03 Track.m4a\n")

    def test_hidden_leading_dots_in_video_filename_are_renamed(self):
        source = self.media(
            "Britney Spears/Unknown Album/...Baby One More Time.m4v",
            Tags(
                title="...Baby One More Time",
                artist="Britney Spears",
                media_type=6,
                title_id=307560076,
            ),
        )
        source.with_suffix(".jpg").write_bytes(b"cover")
        self.db.index_file(source, "music-videos", "307560076")
        playlist = self.library / "Playlists/Me/Videos.m3u8"
        playlist.parent.mkdir(parents=True)
        playlist.write_text(
            "#EXTM3U\n" + os.path.relpath(source, playlist.parent) + "\n"
        )
        expected = source.with_name("_..Baby One More Time.m4v")
        before = source.read_bytes()

        preview = self.invoke("--dry-run")
        self.assertEqual(preview.exit_code, 0, preview.output)
        self.assertIn("...Baby One More Time.m4v ->", preview.output)
        self.assertIn("_..Baby One More Time.m4v", preview.output)
        self.assertTrue(source.is_file())
        self.assertFalse(expected.exists())

        result = self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(source.exists())
        self.assertEqual(expected.read_bytes(), before)
        self.assertEqual(expected.with_suffix(".jpg").read_bytes(), b"cover")
        self.assertEqual(self.db.get(expected)["media_id"], "307560076")
        self.assertIsNone(self.db.get(source))
        self.assertEqual(
            playlist.read_text(),
            "#EXTM3U\n" + os.path.relpath(expected, playlist.parent) + "\n",
        )
        repeat = self.invoke()
        self.assertEqual(repeat.exit_code, 0, repeat.output)
        self.assertIn("moved 0", repeat.output)

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

    def test_multiple_videos_keep_numbering_sidecars_index_and_playlists(self):
        tags = Tags(title="Video", artist="Artist", media_type=6)
        first = self.media("raw/Video.m4v", Tags(**{**vars(tags), "title_id": 123}))
        second = self.media("raw/Video 2.m4v", Tags(**{**vars(tags), "title_id": 456}))
        second.with_suffix(".jpg").write_bytes(b"artwork")
        self.db.index_file(first, "music-videos", "123")
        self.db.index_file(second, "music-videos", "456")
        first_data, second_data = first.read_bytes(), second.read_bytes()
        playlist = self.library / "Playlists" / "Me" / "Videos.m3u8"
        playlist.parent.mkdir(parents=True)
        playlist.write_text(
            "#EXTM3U\n" + os.path.relpath(second, playlist.parent) + "\n"
        )
        preview = self.invoke("--dry-run")
        self.assertEqual(preview.exit_code, 0, preview.output)
        self.assertIn("would move 2", preview.output)
        result = self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("moved 2", result.output)
        expected_first = self.library / "Artist/Unknown Album/Video.m4v"
        expected_second = self.library / "Artist/Unknown Album/Video 2.m4v"
        self.assertEqual(expected_first.read_bytes(), first_data)
        self.assertEqual(expected_second.read_bytes(), second_data)
        self.assertEqual(expected_second.with_suffix(".jpg").read_bytes(), b"artwork")
        self.assertEqual(self.db.get(expected_first)["media_id"], "123")
        self.assertEqual(self.db.get(expected_second)["media_id"], "456")
        self.assertEqual(
            playlist.read_text(),
            "#EXTM3U\n" + os.path.relpath(expected_second, playlist.parent) + "\n",
        )
        repeated = self.invoke()
        self.assertEqual(repeated.exit_code, 0, repeated.output)
        self.assertIn("moved 0", repeated.output)

    def test_video_destination_skips_occupied_sidecars(self):
        first = self.media("raw/Video.m4v", Tags(title="Video", artist="Artist"))
        first.with_suffix(".jpg").write_bytes(b"new artwork")
        occupied_art = self.library / "Artist/Unknown Album/Video.jpg"
        occupied_art.parent.mkdir(parents=True)
        occupied_art.write_bytes(b"existing artwork")
        result = self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(occupied_art.read_bytes(), b"existing artwork")
        target = self.library / "Artist/Unknown Album/Video 2.m4v"
        self.assertTrue(target.is_file())
        self.assertEqual(target.with_suffix(".jpg").read_bytes(), b"new artwork")

    def test_video_can_reuse_slot_released_earlier_in_same_plan(self):
        leaving = self.media(
            "Artist/Unknown Album/Title.m4v",
            Tags(title="Other", artist="Artist", title_id=111),
        )
        arriving = self.media(
            "raw/incoming.m4v", Tags(title="Title", artist="Artist", title_id=222)
        )
        leaving_data, arriving_data = leaving.read_bytes(), arriving.read_bytes()
        result = self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("moved 2", result.output)
        self.assertEqual(
            (self.library / "Artist/Unknown Album/Other.m4v").read_bytes(),
            leaving_data,
        )
        self.assertEqual(leaving.read_bytes(), arriving_data)
        self.assertFalse(arriving.exists())
        self.assertFalse((self.library / "Artist/Unknown Album/Title 2.m4v").exists())

    def test_video_cycle_fails_safely_without_overwriting(self):
        first = self.media(
            "Artist/Unknown Album/A.m4v", Tags(title="B", artist="Artist")
        )
        second = self.media(
            "Artist/Unknown Album/B.m4v", Tags(title="A", artist="Artist")
        )
        originals = (first.read_bytes(), second.read_bytes())
        result = self.invoke()
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("collisions 2", result.output)
        self.assertEqual((first.read_bytes(), second.read_bytes()), originals)

    def test_organize_changes_only_media_references_not_playlist_headers_or_names(self):
        old_song = self.media(
            "raw/song.m4a",
            Tags(title="Song", artist="Band", title_id=555),
        )
        playlist = self.library / "misc" / "original.m3u8"
        playlist.parent.mkdir()
        playlist.write_text(
            "#EXTM3U\n#PLAYLIST:Road/Trip\n"
            "#Gamdl-PLAYLIST-ARTIST:DJ\n"
            "#Gamdl-APPLE-MUSIC-ID:p.123\n"
            "#EXTINF:10,Sample\n" + os.path.relpath(old_song, playlist.parent) + "\n",
        )
        expected_song = self.library / "Band/Unknown Album/Song.m4a"
        preview = self.invoke("--dry-run")
        self.assertEqual(preview.exit_code, 0, preview.output)
        self.assertNotIn("misc/original.m3u8 ->", preview.output)
        self.assertTrue(playlist.exists())
        result = self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(playlist.exists())
        self.assertTrue(expected_song.exists())
        self.assertEqual(
            playlist.read_text(),
            "#EXTM3U\n#PLAYLIST:Road/Trip\n"
            "#Gamdl-PLAYLIST-ARTIST:DJ\n"
            "#Gamdl-APPLE-MUSIC-ID:p.123\n"
            "#EXTINF:10,Sample\n"
            + os.path.relpath(expected_song, playlist.parent)
            + "\n",
        )
        again = self.invoke()
        self.assertEqual(again.exit_code, 0, again.output)
        self.assertIn("Playlists updated: 0", again.output)

    def test_playlist_path_and_header_are_not_derived_from_existing_metadata(self):
        playlist = self.library / "old" / "List.m3u8"
        playlist.parent.mkdir()
        path = self.media("Audio/file.m4a")
        playlist.write_text(
            "#EXTM3U\n#PLAYLIST:List\n#Gamdl-PLAYLIST-ARTIST:Me\n"
            + os.path.relpath(path, playlist.parent)
            + "\n",
        )
        original = playlist.read_bytes()
        result = self.invoke("--playlists-only")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(playlist.exists())
        self.assertEqual(playlist.read_bytes(), original)
        self.assertFalse((self.library / "Playlists/Me/List.m3u8").exists())

    def test_reference_repair_preserves_bom_crlf_and_header_bytes(self):
        media = self.media("Singer/Album/02 Great Song.m4a")
        playlist = self.library / "Playlists/Example/Old.m3u8"
        playlist.parent.mkdir(parents=True)
        header = (
            b"\xef\xbb\xbf#EXTM3U\r\n#PLAYLIST:Still the same name\r\n"
            b"#Gamdl-PLAYLIST-ARTIST:Still the same curator\r\n"
            b"#Gamdl-APPLE-MUSIC-ID:pl.example\r\n"
            b"#EXTINF:180,Still the same song\r\n"
        )
        missing = b"../../Singer/Album/01 Great Song.m4a\r\n"
        playlist.write_bytes(header + missing)

        preview = self.invoke("--playlists-only", "--dry-run")
        self.assertEqual(preview.exit_code, 0, preview.output)
        self.assertIn("references repairable: 1", preview.output)
        self.assertEqual(playlist.read_bytes(), header + missing)

        result = self.invoke("--playlists-only")
        self.assertEqual(result.exit_code, 0, result.output)
        expected = os.path.relpath(media, playlist.parent).encode("utf-8")
        self.assertEqual(playlist.read_bytes(), header + expected + b"\r\n")
        self.assertEqual(media.name, "02 Great Song.m4a")

        repeat = self.invoke("--playlists-only")
        self.assertEqual(repeat.exit_code, 0, repeat.output)
        self.assertIn("Playlists updated: 0", repeat.output)

    def test_playlist_metadata_collisions_do_not_affect_organize(self):
        playlist = self.library / "old" / "original.m3u8"
        playlist.parent.mkdir()
        playlist.write_text("#EXTM3U\n#PLAYLIST:List\n#GAMDL-PLAYLIST-ARTIST:Me\n")
        target = self.library / "Playlists/Me/List.m3u8"
        target.parent.mkdir(parents=True)
        target.write_text("#EXTM3U\n#PLAYLIST:Other\n")
        result = self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("collisions 0", result.output)
        self.assertTrue(playlist.exists())
        self.assertEqual(
            playlist.read_text(), "#EXTM3U\n#PLAYLIST:List\n#GAMDL-PLAYLIST-ARTIST:Me\n"
        )
        self.assertEqual(target.read_text(), "#EXTM3U\n#PLAYLIST:Other\n")
