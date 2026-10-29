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
    def test_cover_format_yaml_cli_and_raw_rejection(self):
        from gamdl.cli.config.loader import click_default_map

        self.assertEqual(click_default_map(load_config(None))["cover_format"], "jpg")
        self._write("output:\n  cover_format: png\n  save_cover: true\n")
        configured = load_config(self.path)
        self.assertEqual(configured.output.cover_format, "png")
        self.assertEqual(click_default_map(configured)["cover_format"], "png")

        captured = []

        async def fake_run(options):
            captured.append(options)
            return 0

        with patch("gamdl.cli.commands.runner.run_rip", side_effect=fake_run):
            runner = CliRunner()
            result = runner.invoke(
                main,
                [
                    "--config",
                    str(self.path),
                    "rip",
                    "https://music.apple.com/br/song/x/1",
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(captured[-1].cover_format, "png")
            self.assertTrue(captured[-1].save_cover)
            result = runner.invoke(
                main,
                [
                    "--config",
                    str(self.path),
                    "rip",
                    "https://music.apple.com/br/song/x/1",
                    "--cover-format",
                    "jpg",
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(captured[-1].cover_format, "jpg")
            for command in ("rip", "library", "search"):
                # Click must reject the removed raw artwork URL trick.
                result = runner.invoke(
                    main,
                    ["--no-config", command, "--cover-format", "raw"],
                )
                self.assertNotEqual(result.exit_code, 0, result.output)
                self.assertIn("Invalid value", result.output)

        self._write("output:\n  cover_format: raw\n")
        with self.assertRaisesRegex(ConfigError, "output.cover_format"):
            load_config(self.path)

    def test_configured_temp_path_and_cli_override(self):
        from gamdl.cli.config.loader import click_default_map

        self.assertIsNone(click_default_map(load_config(None))["temp_path"])
        self._write("output:\n  temp_path: /mnt/staging\n")
        self.assertEqual(
            click_default_map(load_config(self.path))["temp_path"], "/mnt/staging"
        )
        captured = []

        async def fake_run(options):
            captured.append(options)
            return 0

        with patch("gamdl.cli.commands.runner.run_rip", side_effect=fake_run):
            result = CliRunner().invoke(
                main,
                [
                    "--config",
                    str(self.path),
                    "rip",
                    "https://music.apple.com/us/song/example/123",
                    "--temp-path",
                    "/other/staging",
                ],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured[0].temp_path, "/other/staging")

    def test_negative_cli_flags_preserve_positive_yaml_defaults(self):
        from gamdl.cli.config.loader import click_default_map

        defaults = click_default_map(load_config(None))
        self.assertFalse(defaults["no_lyrics"])
        self.assertFalse(defaults["fail_fast"])
        self.assertFalse(defaults["no_progress"])
        self._write(
            "output:\n  save_lyrics: false\ndownload:\n  continue_on_error: false\nui:\n  progress: false\n"
        )
        overrides = click_default_map(load_config(self.path))
        self.assertTrue(overrides["no_lyrics"])
        self.assertTrue(overrides["fail_fast"])
        self.assertTrue(overrides["no_progress"])

        received = []

        async def fake_run(options):
            received.append(options)
            return 0

        with patch("gamdl.cli.commands.runner.run_rip", side_effect=fake_run):
            result = CliRunner().invoke(
                main,
                [
                    "--no-config",
                    "rip",
                    "https://music.apple.com/br/song/test/1",
                    "--no-lyrics",
                    "--fail-fast",
                    "--no-progress",
                ],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(received[0].no_lyrics)
        self.assertTrue(received[0].fail_fast)
        self.assertTrue(received[0].no_progress)

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
        path_result = runner.invoke(
            main, ["--config", str(self.path), "config", "path"]
        )
        self.assertEqual(path_result.stdout.strip(), str(self.path))
        self.assertIn("GAMDL 4.0", path_result.stderr)
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

    def test_invalid_yaml_version_options_are_not_deleted(self):
        for text in (
            "not: [valid",
            "config_version: 2\n",
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
