"""Search option validation and shared rip-option wiring."""

import unittest
from unittest.mock import patch

from click.testing import CliRunner

from gamdl.cli.main import main


class SearchCliTests(unittest.TestCase):
    def test_itunes_is_a_supported_backend_and_uploaded_videos_are_rejected(self):
        captured = []

        async def fake_run(options):
            captured.append(options)
            return 0

        with patch("gamdl.cli.commands.runner.run_rip", side_effect=fake_run):
            result = CliRunner().invoke(
                main,
                [
                    "--no-config",
                    "search",
                    "Caravan Palace",
                    "--search-api",
                    "itunes",
                    "--type",
                    "music-video",
                    "--feeling-lucky",
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(captured[-1].search_api, "itunes")
            self.assertEqual(captured[-1].search_type, "music-video")
            result = CliRunner().invoke(
                main,
                [
                    "--no-config",
                    "search",
                    "Test",
                    "--search-api",
                    "itunes",
                    "--type",
                    "uploaded-video",
                    "--feeling-lucky",
                ],
            )
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("does not support uploaded videos", result.output)
            self.assertEqual(len(captured), 1)

            invalid = CliRunner().invoke(
                main,
                ["--no-config", "search", "Test", "--search-api", "invalid"],
            )
            self.assertNotEqual(invalid.exit_code, 0)
            self.assertIn("Invalid value", invalid.output)

    def test_lucky_requires_type(self):
        result = CliRunner().invoke(
            main, ["--no-config", "search", "hello", "--feeling-lucky"]
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--type", result.output)

    def test_one_term_is_required(self):
        result = CliRunner().invoke(main, ["--no-config", "search"])
        self.assertNotEqual(result.exit_code, 0)

    def test_lucky_wires_rip_options(self):
        received = []

        async def fake_run(options):
            received.append(options)
            return 0

        with patch("gamdl.cli.commands.runner.run_rip", side_effect=fake_run):
            result = CliRunner().invoke(
                main,
                [
                    "--no-config",
                    "search",
                    "Billie Jean",
                    "--type",
                    "song",
                    "--feeling-lucky",
                    "--no-lyrics",
                    "--song-codec",
                    "alac",
                ],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(received[0].search_term, "Billie Jean")
        self.assertEqual(received[0].search_type, "song")
        self.assertTrue(received[0].feeling_lucky)
        self.assertTrue(received[0].no_lyrics)
        self.assertEqual(received[0].song_codec, "alac")
