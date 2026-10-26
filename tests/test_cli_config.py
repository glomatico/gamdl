"""No-network tests of YAML config, profiles, Click defaults and CLI wiring."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from gamdl.cli.config.loader import ConfigError, load_config
from gamdl.cli.main import main


class CliConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "config.yaml"

    def _write(self, text):
        self.path.write_text(text, encoding="utf-8")

    def test_init_validate_show_and_do_not_rewrite_file(self):
        runner = CliRunner()
        initialized = runner.invoke(
            main, ["--config", str(self.path), "config", "init"]
        )
        self.assertEqual(initialized.exit_code, 0, initialized.output)
        content = self.path.read_text()
        self.assertEqual(yaml.safe_load(content)["config_version"], 1)
        self.assertEqual(
            runner.invoke(
                main, ["--config", str(self.path), "config", "validate"]
            ).exit_code,
            0,
        )
        self.assertEqual(
            runner.invoke(
                main, ["--config", str(self.path), "config", "path"]
            ).output.strip(),
            str(self.path),
        )
        self.assertEqual(
            runner.invoke(
                main, ["--config", str(self.path), "config", "init"]
            ).exit_code,
            1,
        )
        self.assertEqual(self.path.read_text(), content)
        show = runner.invoke(main, ["--config", str(self.path), "config", "show"])
        self.assertEqual(show.exit_code, 0, show.output)
        self.assertIn("song_codec_priority:", show.output)
        self.assertEqual(self.path.read_text(), content)

    def test_profiles_and_click_cli_and_environment_precedence(self):
        self._write(
            "config_version: 1\n"
            "download:\n  concurrency: 3\n  song_codec_priority: [aac-web]\n"
            "output:\n  save_cover: true\n"
            "profiles:\n  archive:\n    download:\n      concurrency: 5\n"
            "      song_codec_priority: [alac, aac-web]\n"
        )
        calls = []

        async def fake_run(options):
            calls.append(options)
            return 0

        with patch("gamdl.cli.commands.runner.run_rip", side_effect=fake_run):
            runner = CliRunner()
            result = runner.invoke(
                main,
                [
                    "--config",
                    str(self.path),
                    "--profile",
                    "archive",
                    "rip",
                    "https://music.apple.com/br/song/title/123",
                    "--no-progress",
                    "--concurrency",
                    "7",
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(calls[-1].concurrency, 7)
            self.assertEqual(calls[-1].song_codec_priority, ("alac", "aac-web"))
            self.assertTrue(calls[-1].save_cover)
            result = runner.invoke(
                main,
                [
                    "--config",
                    str(self.path),
                    "rip",
                    "https://music.apple.com/br/song/title/123",
                ],
                env={"GAMDL_RIP_CONCURRENCY": "9"},
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(calls[-1].concurrency, 9)
            self.assertEqual(calls[-1].song_codec_priority, ("aac-web",))
            result = runner.invoke(
                main,
                ["--no-config", "rip", "https://music.apple.com/br/song/title/123"],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(calls[-1].concurrency, 8)

    def test_invalid_yaml_version_options_and_unknown_keys_are_not_deleted(self):
        for text in (
            "not: [valid",
            "config_version: 2\n",
            "download:\n  mystery_flag: 1\n",
            "download:\n  concurrency: -1\n",
            "download:\n  concurrent_downloads: true\n",
            "output:\n  save_cover: yes\n",  # YAML 1.1 resolves to bool, valid
            "profiles:\n  broken:\n    output:\n      album_folder_template: '{__class__}'\n",
            "download:\n  concurrency: 3\ndownload:\n  concurrency: 4\n",
        ):
            if text.startswith("output:\n  save_cover: yes"):
                continue
            with self.subTest(text=text):
                self._write(text)
                with self.assertRaises(ConfigError):
                    load_config(self.path)
                self.assertEqual(self.path.read_text(), text)

    def test_explicit_overrides_validation_and_profiles(self):
        self._write(
            "profiles:\n  fast:\n    download:\n      concurrent_downloads: 4\n"
        )
        self.assertEqual(
            load_config(self.path, profile="fast").download.concurrent_downloads, 4
        )
        with self.assertRaisesRegex(ConfigError, "Unknown profile"):
            load_config(self.path, profile="absent")
        self.assertEqual(load_config(None).download.concurrent_downloads, 2)

    def test_validation_rejects_explicitly_missing_file(self):
        result = CliRunner().invoke(
            main, ["--config", str(self.path), "config", "validate"]
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("does not exist", result.output)

    def test_legacy_defaults_and_missing_prd_are_valid_for_interactive_setup(self):
        config = load_config(None)
        self.assertTrue(config.output.save_lyrics)
        self.assertFalse(config.output.save_playlist)
        self.assertEqual(config.download.song_codec_priority, ["aac-web"])
        self._write("auth:\n  drm_backend: playready\n")
        self.assertEqual(load_config(self.path).auth.drm_backend, "playready")

    def test_invalid_click_option_uses_same_error_style(self):
        result = CliRunner().invoke(main, ["rip", "URL", "--song-codec", "invalid"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("✗", result.output)
        self.assertIn("--song-codec", result.output)
        self.assertNotIn("Usage: gamdl", result.output)

    def test_explicit_ask_codec_conflict_is_a_click_usage_error(self):
        with patch("gamdl.cli.commands.runner.interactive", return_value=True):
            result = CliRunner().invoke(
                main,
                [
                    "--no-config",
                    "rip",
                    "URL",
                    "--ask-song-codec",
                    "--song-codec",
                    "alac",
                ],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--ask-song-codec conflicts", result.output)
