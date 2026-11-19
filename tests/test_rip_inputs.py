"""Offline tests for automatic URL file expansion in the rip command."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from gamdl.cli.commands.input_urls import expand_rip_inputs
from gamdl.cli.main import main


URL = "https://music.apple.com/us/song/test/123456789"
ISRC = "isrc:USSM19902991"


class RipInputsTests(unittest.TestCase):
    def test_preserves_order_and_duplicates_mixed_with_files(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "playlist without extension"
            path.write_text(
                f"\ufeff# A URL list\n\n {ISRC}  \n{URL}\n{URL}\n",
                encoding="utf-8",
            )
            self.assertEqual(
                expand_rip_inputs((URL, str(path), ISRC)),
                (URL, ISRC, URL, URL, ISRC),
            )

    def test_supports_multiple_files_and_case_insensitive_prefixes(self):
        with tempfile.TemporaryDirectory() as temp:
            a = Path(temp) / "a.txt"
            b = Path(temp) / "b.txt"
            a.write_text("ISRC:USSM19902991\n", encoding="utf-8")
            b.write_text("HTTP://music.apple.com/us/song/test/1\n", encoding="utf-8")
            self.assertEqual(
                expand_rip_inputs((str(a), str(b), URL)),
                ("ISRC:USSM19902991", "HTTP://music.apple.com/us/song/test/1", URL),
            )

    def test_missing_path_and_directory_get_requested_error(self):
        with tempfile.TemporaryDirectory() as temp:
            for path in ("not-a-url", str(Path(temp) / "missing.txt"), temp):
                with self.subTest(path=path):
                    with self.assertRaisesRegex(
                        Exception, "Invalid URL or file containing URLs not found"
                    ):
                        expand_rip_inputs((path,))

    def test_empty_unreadable_and_nested_file_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "songs.txt"
            path.write_text("\ufeff# comments only\n \n", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "No URLs or ISRCs found in file"):
                expand_rip_inputs((str(path),))
            path.write_bytes(b"\xff")
            with self.assertRaisesRegex(Exception, "Could not read URL file"):
                expand_rip_inputs((str(path),))
            path.write_text("other.txt\n", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "Invalid URL or ISRC.*line 1"):
                expand_rip_inputs((str(path),))

    def test_rip_cli_expands_before_starting_download(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "my list.txt"
            path.write_text(f"{URL}\n{ISRC}\n", encoding="utf-8")
            received = []

            async def fake_run(options):
                received.append(options.urls)
                return 0

            with patch("gamdl.cli.commands.runner.run_rip", side_effect=fake_run):
                result = CliRunner().invoke(
                    main, ["--no-config", "rip", ISRC, str(path), URL]
                )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(received, [(ISRC, URL, ISRC, URL)])

    def test_invalid_rip_file_does_not_start_download(self):
        with (
            patch("gamdl.cli.commands.runner.run_rip") as run,
            tempfile.TemporaryDirectory() as temp,
        ):
            result = CliRunner().invoke(
                main, ["--no-config", "rip", str(Path(temp) / "missing.txt")]
            )
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn(
                "Invalid URL or file containing URLs not found", result.output
            )
            run.assert_not_called()

    def test_only_rip_expands_files(self):
        async def fake_run(options):
            return 0

        with (
            patch(
                "gamdl.cli.commands.rip.expand_rip_inputs", side_effect=AssertionError
            ),
            patch("gamdl.cli.commands.runner.run_rip", side_effect=fake_run),
            patch("gamdl.cli.commands.search.require_terminal"),
            patch("gamdl.cli.commands.library.require_terminal"),
        ):
            for command in (["search", "songs.txt"], ["library"]):
                with self.subTest(command=command):
                    result = CliRunner().invoke(main, ["--no-config", *command])
                    self.assertEqual(result.exit_code, 0, result.output)


if __name__ == "__main__":
    unittest.main()
