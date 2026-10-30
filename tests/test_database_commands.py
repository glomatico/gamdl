"""Offline database command tests."""

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from click.testing import CliRunner

from gamdl.cli.main import main


class DatabaseCommandTests(TestCase):
    def test_scan_and_prune_and_disabled_warning(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            output.mkdir()
            song = output / "track.m4a"
            song.write_bytes(b"fake")
            unknown = output / "unknown.mp4"
            unknown.write_bytes(b"fake")
            db = root / "index.sqlite3"
            config = root / "config.yaml"
            config.write_text(
                f"database:\n  path: {db}\noutput:\n  directory: {output}\n"
            )
            runner = CliRunner()
            with patch(
                "gamdl.cli.commands.database._native.read_mp4_title_id",
                return_value="123456",
            ):
                args = ["--config", str(config), "database"]
                dry = runner.invoke(main, [*args, "scan", "--dry-run"])
                self.assertEqual(dry.exit_code, 0, dry.output)
                self.assertIn("added 1", dry.output)
                self.assertIn("ambiguous 1", dry.output)
                self.assertIn("disabled", dry.output)
                self.assertFalse(db.exists())
                first = runner.invoke(main, [*args, "scan"])
                self.assertEqual(first.exit_code, 0, first.output)
                self.assertIn("added 1", first.output)
                again = runner.invoke(main, [*args, "scan"])
                self.assertEqual(again.exit_code, 0, again.output)
                self.assertIn("unchanged 1", again.output)
                info = runner.invoke(main, [*args, "info"])
                self.assertIn("Records: 1", info.output)
                with sqlite3.connect(db) as conn:
                    row = conn.execute(
                        "SELECT media_type, media_id FROM media_files"
                    ).fetchone()
                self.assertEqual(row, ("songs", "123456"))
                song.unlink()
                dry_prune = runner.invoke(main, [*args, "prune", "--dry-run"])
                self.assertIn("Missing-file records: 1", dry_prune.output)
                self.assertIn("Records: 1", runner.invoke(main, [*args, "info"]).output)
                prune = runner.invoke(main, [*args, "prune", "--yes"])
                self.assertEqual(prune.exit_code, 0, prune.output)
                self.assertIn("Records: 0", runner.invoke(main, [*args, "info"]).output)
