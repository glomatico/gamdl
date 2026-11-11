"""Fresh portable SQLite catalog, source ranking and offline scan tests."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import patch

from click.testing import CliRunner

from gamdl import _native
from gamdl.cli.config.models import AuthConfig, DatabaseConfig
from gamdl.cli.config.models import config_path as default_config_path
from gamdl.cli.config.models import database_path as default_database_path
from gamdl.cli.main import main
from gamdl.download.database import TAG_NAMES, MediaDatabase
from gamdl.download.enums import DatabaseStatus
from gamdl.download.models import DownloadResult
from gamdl.download.tags import Tags
from gamdl.muxer import Muxer


class DatabaseCatalogTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = self.root / "Apple Music"
        self.library.mkdir()
        self.path = self.root / "media.sqlite3"

    def db(self, **kwargs) -> MediaDatabase:
        return MediaDatabase(self.path, root=self.library, **kwargs)

    def media(self, name: str, content: bytes = b"audio") -> Path:
        path = self.library / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_default_files_share_home_gamdl_directory(self):
        folder = Path.home() / ".gamdl"
        self.assertEqual(default_config_path(), folder / "config.yml")
        self.assertEqual(default_database_path(), folder / "media.sqlite3")
        self.assertEqual(DatabaseConfig().path, str(folder / "media.sqlite3"))
        self.assertEqual(AuthConfig().cookies_path, str(folder / "cookies.txt"))

    def test_schema_exact_tag_field_sets_and_fresh_database(self):
        created = []
        db = self.db(save_metadata=True, on_created=created.append)
        song = self.media("song.m4a")
        self.assertEqual(db.index_file(song, "songs", "123").value, "inserted")
        self.assertEqual(created, [self.path])
        self.assertEqual(db.index_file(song, "songs", "123").value, "unchanged")
        self.assertEqual(created, [self.path])
        self.assertTrue(
            db.upsert_metadata(
                song,
                "songs",
                Tags(
                    title="Unchanged",
                    artist="Artist",
                    album_id=12,
                    genre=("Rock", "Pop"),
                    explicit=False,
                ),
            )
        )
        self.assertFalse(db.upsert_metadata(song, "songs", {"title": "Overwrite"}))
        self.assertTrue(db.upsert_metadata(song, "songs", {"album": "Album"}))
        row = db.get_metadata(song, "songs")
        self.assertEqual(row["title"], "Unchanged")
        self.assertEqual(row["album"], "Album")
        self.assertEqual(row["album_id"], "12")
        self.assertEqual(json.loads(row["genre"]), ["Rock", "Pop"])
        self.assertEqual(row["explicit"], 0)

        video = self.media("video.m4v")
        db.index_file(video, "music-videos", "777")
        db.upsert_metadata(video, "music-videos", Tags(title="Video"))
        uploaded = self.media("upload.mp4")
        db.index_file(uploaded, "uploaded-videos", "456")
        db.upsert_metadata(uploaded, "uploaded-videos", Tags(title="Upload"))
        expected = {field.name for field in fields(Tags)} - {"lyrics"}
        self.assertEqual(set(TAG_NAMES), expected)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(
                connection.execute(
                    "SELECT path FROM media_files WHERE media_id='123'"
                ).fetchone()[0],
                "song.m4a",
            )
            for table in ("songs", "music_videos"):
                columns = {
                    row[1] for row in connection.execute(f"PRAGMA table_info({table})")
                } - {"file_id"}
                self.assertEqual(columns, expected)
            uploaded_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(uploaded_videos)")
            } - {"file_id"}
            self.assertEqual(
                uploaded_columns,
                {
                    "title",
                    "artist",
                    "title_id",
                    "release_date",
                    "storefront",
                    "media_type",
                },
            )
            self.assertNotIn("xid", uploaded_columns)
        self.assertEqual(
            db.update_source(uploaded, "album_tracks", "12").value, "unchanged"
        )
        self.assertIsNone(db.get(uploaded)["source_kind"])

    def test_source_ranking_missing_file_and_identity_replacement(self):
        db = self.db()
        song = self.media("song.m4a")
        db.index_file(song, "songs", "123")
        db.update_source(song, "single_track", "123")
        db.update_source(song, "playlist_tracks", "p.111", 1)
        self.assertEqual(db.get(song)["source_id"], "p.111")
        db.update_source(song, "album_tracks", "456", 2)
        db.update_source(song, "playlist_tracks", "p.222", 3)
        db.update_source(song, "album_tracks", "999", 7)
        self.assertEqual(
            tuple(
                db.get(song)[key]
                for key in ("source_kind", "source_id", "source_index")
            ),
            ("album_tracks", "456", 2),
        )
        self.assertIsNotNone(db.find_by_media_id("songs", "123"))
        song.write_bytes(b"changed-size")
        self.assertIsNone(db.find_by_media_id("songs", "123"))
        db.index_file(song, "songs", "789")
        self.assertIsNone(db.get(song)["source_kind"])
        self.assertEqual(db.get(song)["media_id"], "789")

    def test_old_sqlite_rejected_without_modification(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE media_files (path TEXT PRIMARY KEY, media_id TEXT)"
            )
        original = self.path.read_bytes()
        db = self.db()
        with self.assertRaisesRegex(ValueError, "Unsupported Gamdl database schema"):
            db.index_file(self.media("song.m4a"), "songs", "123")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(self.path.with_name(self.path.name + "-wal").exists())

    def test_external_media_is_not_indexed_or_reported_unchanged(self):
        outside = self.root / "outside.m4a"
        outside.write_bytes(b"audio")
        db = self.db()
        with self.assertWarnsRegex(UserWarning, "outside the database output"):
            status = db.record(
                None, DownloadResult("123", outside, "downloaded", 5, None)
            )
        self.assertEqual(status, DatabaseStatus.SKIPPED)
        self.assertFalse(db.path.exists())

    def test_parallel_file_registration_is_safe(self):
        db = self.db()
        files = [self.media(f"track-{i}.m4a") for i in range(32)]
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(
                    lambda indexed: db.index_file(indexed[1], "songs", str(indexed[0])),
                    enumerate(files),
                )
            )
        self.assertEqual(len(results), 32)
        self.assertEqual(len(db.local_records(self.library)), 32)

    def test_first_run_config_is_only_created_once(self):
        config = self.root / "first-run" / "config.yaml"
        args = ["--config", str(config), "database", "info"]
        runner = CliRunner()
        first = runner.invoke(main, args)
        self.assertEqual(first.exit_code, 0, first.output)
        self.assertIn(f"Created config at {config}", first.output)
        self.assertTrue(config.is_file())
        second = runner.invoke(main, args)
        self.assertEqual(second.exit_code, 0, second.output)
        self.assertNotIn("Created config at", second.output)
        no_config = self.root / "disabled-config.yaml"
        without = runner.invoke(
            main, ["--config", str(no_config), "--no-config", "database", "info"]
        )
        self.assertEqual(without.exit_code, 0, without.output)
        self.assertFalse(no_config.exists())
        self.assertNotIn("Created config at", without.output)

    @skipUnless(shutil.which("ffmpeg"), "FFmpeg is needed for MP4 tag fixture")
    def test_native_reader_recovers_local_tags_without_cover_or_lyrics(self):
        song = self.library / "tags.m4a"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=duration=0.05",
                "-c:a",
                "aac",
                "-y",
                str(song),
            ],
            capture_output=True,
            check=True,
        )
        Muxer().tag(
            song,
            tags=Tags(
                title="Test",
                artist="Artist",
                album="Album",
                title_id=1234,
                album_id=9876,
                track_number=2,
                track_total=9,
                disc_number=1,
                disc_total=1,
                genre=("Pop", "Rock"),
                xid="xid-123",
                storefront=143503,
                rating=4,
                title_sort="Title sort",
            ),
        )
        extracted = dict(_native.read_mp4_organize_tags(str(song)))
        self.assertEqual(extracted["title_id"], 1234)
        self.assertEqual(extracted["album_id"], 9876)
        self.assertEqual(extracted["genre"], ["Pop", "Rock"])
        self.assertEqual(extracted["xid"], "xid-123")
        self.assertEqual(extracted["title_sort"], "Title sort")
        self.assertEqual(extracted["rating"], 4)
        self.assertEqual(extracted["storefront"], 143503)
        self.assertNotIn("lyrics", extracted)
        self.assertNotIn("cover", extracted)
        self.assertNotIn("compilation", extracted)

    def test_scan_rebuilds_playlists_and_complete_albums_without_network(self):
        first = self.media("Thriller/01 Wanna Be Startin Somethin.m4a")
        second = self.media("Thriller/02 Baby Be Mine.m4a")
        incomplete = self.media("Off the Wall/01 Dont Stop.m4a")
        playlist = self.library / "Playlists" / "Hits.m3u8"
        playlist.parent.mkdir()
        playlist.write_text(
            "#EXTM3U\n#PLAYLIST:Hits\n#Gamdl-APPLE-MUSIC-ID:p.abc\n"
            "\n../Off the Wall/01 Dont Stop.m4a\n"
        )
        tags = {
            first: {
                "title": "Wanna Be Startin Somethin",
                "title_id": 101,
                "album_id": 999,
                "disc_number": 1,
                "disc_total": 1,
                "track_number": 1,
                "track_total": 2,
            },
            second: {
                "title": "Baby Be Mine",
                "title_id": 102,
                "album_id": 999,
                "disc_number": 1,
                "disc_total": 1,
                "track_number": 2,
                "track_total": 2,
            },
            incomplete: {
                "title": "Dont Stop",
                "title_id": 201,
                "album_id": 888,
                "disc_number": 1,
                "disc_total": 1,
                "track_number": 1,
                "track_total": 10,
            },
        }
        config = self.root / "config.yaml"

        def config_content(save_metadata: bool) -> str:
            return (
                f"output:\n  directory: '{self.library}'\n"
                f"database:\n  path: '{self.path}'\n"
                f"  save_metadata: {'true' if save_metadata else 'false'}\n"
            )

        config.write_text(config_content(False))
        runner = CliRunner()
        args = ["--config", str(config), "database", "scan"]
        with (
            patch(
                "gamdl.cli.commands.database._native.read_mp4_organize_tags",
                side_effect=lambda path: tags[Path(path)],
            ),
            patch(
                "gamdl.cli.commands.database._native.read_mp4_title_id",
                side_effect=lambda path: str(tags[Path(path)]["title_id"]),
            ),
        ):
            dry = runner.invoke(main, [*args, "--dry-run"])
            self.assertEqual(dry.exit_code, 0, dry.output)
            self.assertIn("complete albums 1", dry.output)
            self.assertFalse(self.path.exists())
            first_scan = runner.invoke(main, args)
            self.assertEqual(first_scan.exit_code, 0, first_scan.output)
            self.assertIn("Created database at", first_scan.output)
            db = self.db()
            self.assertEqual(db.get(first)["source_kind"], "album_tracks")
            self.assertEqual(db.get(second)["source_index"], 1)
            self.assertEqual(db.get(incomplete)["source_kind"], "playlist_tracks")
            self.assertEqual(db.get(incomplete)["source_id"], "p.abc")
            self.assertEqual(db.get(incomplete)["source_index"], 1)
            self.assertIsNone(db.get_metadata(first, "songs"))
            config.write_text(config_content(True))
            enriched = runner.invoke(main, args)
            self.assertEqual(enriched.exit_code, 0, enriched.output)
            self.assertIn("metadata filled 3", enriched.output)
            self.assertEqual(
                self.db().get_metadata(first, "songs")["title"], tags[first]["title"]
            )
            self.assertEqual(self.db().get_metadata(second, "songs")["album_id"], "999")
            repeat = runner.invoke(main, args)
            self.assertEqual(repeat.exit_code, 0, repeat.output)
            self.assertIn("metadata filled 0", repeat.output)
