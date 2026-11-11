"""No-network tests of YAML config, Click defaults and CLI wiring."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from gamdl.cli.config.loader import ConfigError, load_config
from gamdl.cli.main import main


class CliConfigTests(unittest.TestCase):
    def test_music_video_container_defaults_yaml_cli_and_validation(self):
        from gamdl.cli.config.loader import click_default_map

        defaults = load_config(None)
        self.assertEqual(defaults.output.music_video_container, "m4v")
        self.assertEqual(click_default_map(defaults)["music_video_container"], "m4v")
        self._write("output:\n  music_video_container: mp4\n")
        configured = load_config(self.path)
        self.assertEqual(configured.output.music_video_container, "mp4")
        self.assertEqual(click_default_map(configured)["music_video_container"], "mp4")

        received = []

        async def fake_run(options):
            received.append(options)
            return 0

        with (
            patch("gamdl.cli.commands.runner.run_rip", side_effect=fake_run),
            patch("gamdl.cli.commands.library.require_terminal"),
            patch("gamdl.cli.commands.search.require_terminal"),
        ):
            runner = CliRunner()
            for subcommand in (["rip", "URL"], ["library"], ["search", "video"]):
                with self.subTest(subcommand=subcommand):
                    base = ["--config", str(self.path), *subcommand]
                    result = runner.invoke(main, base)
                    self.assertEqual(result.exit_code, 0, result.output)
                    self.assertEqual(received[-1].music_video_container, "mp4")
                    result = runner.invoke(
                        main, [*base, "--music-video-container", "m4v"]
                    )
                    self.assertEqual(result.exit_code, 0, result.output)
                    self.assertEqual(received[-1].music_video_container, "m4v")
                    result = runner.invoke(
                        main,
                        ["--no-config", *subcommand, "--music-video-container", "avi"],
                    )
                    self.assertNotEqual(result.exit_code, 0)
                    self.assertIn("Invalid value", result.output)

        for value in ("avi", "MP4", "", 3, None):
            with self.subTest(invalid_yaml=value):
                self._write(
                    yaml.safe_dump({"output": {"music_video_container": value}})
                )
                with self.assertRaisesRegex(
                    ConfigError, "output.music_video_container"
                ):
                    load_config(self.path)

    def test_search_api_defaults_yaml_and_cli_override(self):
        from gamdl.cli.config.loader import click_default_map

        self.assertEqual(load_config(None).search.api, "apple-music")
        self.assertEqual(
            click_default_map(load_config(None))["search_api"], "apple-music"
        )
        self._write("search:\n  api: itunes\n")
        self.assertEqual(load_config(self.path).search.api, "itunes")
        received = []

        async def fake_run(options):
            received.append(options)
            return 0

        with patch("gamdl.cli.commands.runner.run_rip", side_effect=fake_run):
            cli = CliRunner()
            base = [
                "--config",
                str(self.path),
                "search",
                "Test",
                "--type",
                "song",
                "--feeling-lucky",
            ]
            default = cli.invoke(main, base)
            self.assertEqual(default.exit_code, 0, default.output)
            self.assertEqual(received[-1].search_api, "itunes")
            overridden = cli.invoke(main, [*base, "--search-api", "apple-music"])
            self.assertEqual(overridden.exit_code, 0, overridden.output)
            self.assertEqual(received[-1].search_api, "apple-music")

        self._write("search:\n  api: spotify\n")
        with self.assertRaisesRegex(ConfigError, "search.api"):
            load_config(self.path)
        self.assertEqual(self.path.read_text(), "search:\n  api: spotify\n")

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

    def test_negative_yaml_flags_preserve_existing_default_behavior(self):
        from gamdl.cli.config.loader import click_default_map

        defaults = click_default_map(load_config(None))
        self.assertFalse(defaults["no_lyrics_file"])
        self.assertFalse(defaults["fail_fast"])
        self.assertFalse(defaults["no_progress"])
        self.assertFalse(defaults["no_tracebacks"])
        self._write(
            "output:\n  no_lyrics_file: true\ndownload:\n  fail_fast: true\nui:\n  no_progress: true\n  no_tracebacks: true\n"
        )
        overrides = click_default_map(load_config(self.path))
        self.assertTrue(overrides["no_lyrics_file"])
        self.assertTrue(overrides["fail_fast"])
        self.assertTrue(overrides["no_progress"])
        self.assertTrue(overrides["no_tracebacks"])

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
                    "--no-lyrics-file",
                    "--fail-fast",
                    "--no-progress",
                    "--no-tracebacks",
                ],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(received[0].no_lyrics_file)
        self.assertTrue(received[0].fail_fast)
        self.assertTrue(received[0].no_progress)
        self.assertTrue(received[0].no_tracebacks)

    def test_no_tracebacks_replaces_debug_flag(self):
        runner = CliRunner()
        rip_help = runner.invoke(main, ["--no-config", "rip", "--help"]).output
        self.assertIn("--no-tracebacks", rip_help)
        self.assertNotIn("--debug", rip_help)
        sync_help = runner.invoke(
            main, ["--no-config", "sync", "playlists", "--help"]
        ).output
        self.assertIn("--no-tracebacks", sync_help)
        self.assertNotIn("--debug", sync_help)
        legacy = runner.invoke(
            main, ["--no-config", "rip", "https://music.apple.com/br/song/1", "--debug"]
        )
        self.assertNotEqual(legacy.exit_code, 0)
        self.assertIn("No such option '--debug'", legacy.output)

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
        self.assertIn("Gamdl 4.0", path_result.stderr)
        self.assertIn("# Gamdl 4.0 configuration", content)
        version = runner.invoke(main, ["--version"])
        self.assertEqual(version.exit_code, 0, version.output)
        self.assertIn("Gamdl, version 4.0", version.stdout)
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

    def test_generated_yaml_uses_false_for_every_boolean(self):
        from gamdl.cli.config.loader import click_default_map

        generated = CliRunner().invoke(
            main, ["--config", str(self.path), "config", "init"]
        )
        self.assertEqual(generated.exit_code, 0, generated.output)
        config = yaml.safe_load(self.path.read_text())
        for section, settings in config.items():
            if not isinstance(settings, dict):
                continue
            for name, value in settings.items():
                if type(value) is bool:
                    with self.subTest(setting=f"{section}.{name}"):
                        self.assertFalse(value)
        self.assertFalse(config["output"]["no_lyrics_file"])
        self.assertFalse(config["output"]["no_tag"])
        self.assertFalse(config["download"]["fail_fast"])
        self.assertFalse(config["ui"]["no_progress"])
        self.assertNotIn("save_lyrics", config["output"])
        self.assertNotIn("tag", config["output"])
        self.assertNotIn("continue_on_error", config["download"])
        self.assertNotIn("progress", config["ui"])

        defaults = click_default_map(load_config(self.path))
        self.assertFalse(defaults["no_lyrics_file"])
        self.assertFalse(defaults["no_tag"])
        self.assertFalse(defaults["fail_fast"])
        self.assertFalse(defaults["no_progress"])

    def test_click_cli_and_environment_precedence(self):
        self._write(
            "config_version: 1\n"
            "download:\n  segment_concurrency: 3\n  song_codec_priority: [aac-web]\n"
            "output:\n  save_cover: true\n"
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
                    "rip",
                    "https://music.apple.com/br/song/title/123",
                    "--no-progress",
                    "--segment-concurrency",
                    "7",
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(calls[-1].segment_concurrency, 7)
            self.assertEqual(calls[-1].song_codec_priority, ("aac-web",))
            self.assertTrue(calls[-1].save_cover)
            result = runner.invoke(
                main,
                [
                    "--config",
                    str(self.path),
                    "rip",
                    "https://music.apple.com/br/song/title/123",
                ],
                env={"GAMDL_RIP_SEGMENT_CONCURRENCY": "9"},
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(calls[-1].segment_concurrency, 9)
            self.assertEqual(calls[-1].song_codec_priority, ("aac-web",))
            result = runner.invoke(
                main,
                ["--no-config", "rip", "https://music.apple.com/br/song/title/123"],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(calls[-1].segment_concurrency, 8)

    def test_segment_concurrency_cli_name_without_legacy_alias(self):
        self._write("download:\n  segment_concurrency: 4\n")
        received = []

        async def fake_run(options):
            received.append(options)
            return 0

        with patch("gamdl.cli.commands.runner.run_rip", side_effect=fake_run):
            runner = CliRunner()
            prefix = [
                "--config",
                str(self.path),
                "rip",
                "https://music.apple.com/br/song/123",
            ]
            result = runner.invoke(main, [*prefix, "--segment-concurrency", "11"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(received[-1].segment_concurrency, 11)
            legacy = runner.invoke(main, [*prefix, "--concurrency", "7"])
            self.assertNotEqual(legacy.exit_code, 0)
            self.assertIn("No such option '--concurrency'", legacy.output)
            result = runner.invoke(main, prefix)
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(received[-1].segment_concurrency, 4)
            help_text = runner.invoke(main, ["--no-config", "rip", "--help"]).output
            self.assertIn("--segment-concurrency", help_text)
            self.assertNotIn("--concurrency", help_text)

    def test_legacy_concurrency_yaml_key_is_migrated(self):
        original = "download:\n  concurrency: 13\n"
        self._write(original)
        self.assertEqual(load_config(self.path).download.segment_concurrency, 13)
        rewritten = yaml.safe_load(self.path.read_text())
        self.assertEqual(rewritten["download"]["segment_concurrency"], 13)
        self.assertNotIn("concurrency", rewritten["download"])
        self.assertEqual(self.path.with_name("config.yaml.bak").read_text(), original)

        # When both spellings are supplied, the canonical one wins.
        self._write("download:\n  concurrency: 3\n  segment_concurrency: 15\n")
        self.assertEqual(load_config(self.path).download.segment_concurrency, 15)
        self.assertNotIn(
            "concurrency", yaml.safe_load(self.path.read_text())["download"]
        )

    def test_comma_separated_codec_priorities_and_yaml_lists(self):
        self._write(
            "download:\n"
            "  song_codec_priority:\n"
            "    - alac-hires\n"
            "    - aac-web\n"
            "  music_video_codec_priority:\n"
            "    - h265\n"
            "    - h264\n"
        )
        settings = load_config(self.path)
        self.assertEqual(
            settings.download.song_codec_priority, ["alac-hires", "aac-web"]
        )
        self.assertEqual(settings.download.music_video_codec_priority, ["h265", "h264"])

        received = []

        async def fake_run(options):
            received.append(options)
            return 0

        with (
            patch("gamdl.cli.commands.runner.run_rip", side_effect=fake_run),
            patch("gamdl.cli.commands.library.require_terminal"),
            patch("gamdl.cli.commands.search.require_terminal"),
        ):
            runner = CliRunner()
            for command in (
                ["rip", "URL"],
                ["library"],
                ["search", "example"],
            ):
                with self.subTest(command=command):
                    result = runner.invoke(
                        main,
                        [
                            "--config",
                            str(self.path),
                            *command,
                            "--song-codec-priority",
                            "alac-hires, atmos",
                            "--song-codec-priority",
                            "aac-web",
                            "--music-video-codec-priority",
                            "h265, h264",
                        ],
                    )
                    self.assertEqual(result.exit_code, 0, result.output)
                    self.assertEqual(
                        received[-1].song_codec_priority,
                        ("alac-hires", "atmos", "aac-web"),
                    )
                    self.assertEqual(
                        received[-1].music_video_codec_priority,
                        ("h265", "h264"),
                    )

            result = runner.invoke(main, ["--config", str(self.path), "rip", "URL"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(
                received[-1].song_codec_priority, ("alac-hires", "aac-web")
            )
            self.assertEqual(received[-1].music_video_codec_priority, ("h265", "h264"))

            for option, bad in (
                ("--song-codec-priority", "alac-hires,,aac-web"),
                ("--song-codec-priority", "not-a-codec"),
                ("--music-video-codec-priority", "h264,unknown"),
            ):
                with self.subTest(option=option, bad=bad):
                    result = runner.invoke(
                        main,
                        ["--no-config", "rip", "URL", option, bad],
                    )
                    self.assertNotEqual(result.exit_code, 0)
                    self.assertIn("Invalid value", result.output)

    def test_tag_settings_defaults_yaml_and_cli_overrides(self):
        from gamdl.cli.config.loader import click_default_map

        settings = load_config(None)
        self.assertFalse(settings.output.no_tag)
        self.assertEqual(settings.output.exclude_tags, [])
        self.assertFalse(click_default_map(settings)["no_tag"])
        self.assertEqual(click_default_map(settings)["exclude_tags"], ())

        self._write(
            "output:\n  no_tag: true\n  exclude_tags:\n    - comment\n    - cover\n"
        )
        configured = load_config(self.path)
        self.assertTrue(configured.output.no_tag)
        self.assertEqual(configured.output.exclude_tags, ["comment", "cover"])
        self.assertTrue(click_default_map(configured)["no_tag"])
        self.assertEqual(
            click_default_map(configured)["exclude_tags"], ("comment", "cover")
        )

        received = []

        async def fake_run(options):
            received.append(options)
            return 0

        with (
            patch("gamdl.cli.commands.runner.run_rip", side_effect=fake_run),
            patch("gamdl.cli.commands.library.require_terminal"),
            patch("gamdl.cli.commands.search.require_terminal"),
        ):
            runner = CliRunner()
            for command in (["rip", "URL"], ["library"], ["search", "example"]):
                with self.subTest(command=command):
                    base = ["--config", str(self.path), *command]
                    result = runner.invoke(main, base)
                    self.assertEqual(result.exit_code, 0, result.output)
                    self.assertTrue(received[-1].no_tag)
                    self.assertEqual(received[-1].exclude_tags, ("comment", "cover"))

                    result = runner.invoke(
                        main, [*base, "--tag", "--exclude-tags", "artist,genre"]
                    )
                    self.assertEqual(result.exit_code, 0, result.output)
                    self.assertFalse(received[-1].no_tag)
                    self.assertEqual(received[-1].exclude_tags, ("artist,genre",))

                    result = runner.invoke(main, ["--no-config", *command, "--no-tag"])
                    self.assertEqual(result.exit_code, 0, result.output)
                    self.assertTrue(received[-1].no_tag)

    def test_tag_settings_invalid_yaml_is_rejected_without_rewrite(self):
        for text in (
            "output:\n  no_tag: null\n",
            "output:\n  no_tag: yes\n",  # YAML boolean true is valid
            "output:\n  no_tag: disabled\n",
            "output:\n  exclude_tags: comment,cover\n",
            "output:\n  exclude_tags: [comment, missing_tag]\n",
            "output:\n  exclude_tags: [comment, comment]\n",
            "output:\n  exclude_tags: [42]\n",
        ):
            if text == "output:\n  no_tag: yes\n":
                self._write(text)
                self.assertTrue(load_config(self.path).output.no_tag)
                continue
            with self.subTest(text=text):
                self._write(text)
                with self.assertRaisesRegex(
                    ConfigError, "output.(no_tag|exclude_tags)"
                ):
                    load_config(self.path)
                self.assertEqual(self.path.read_text(), text)

        self._write("output:\n  exclude_tags: []\n")
        self.assertEqual(load_config(self.path).output.exclude_tags, [])

    def test_invalid_yaml_version_options_are_not_deleted(self):
        for text in (
            "not: [valid",
            "config_version: 2\n",
            "download:\n  segment_concurrency: -1\n",
            "download:\n  concurrent_downloads: true\n",
            "output:\n  save_cover: yes\n",  # YAML 1.1 resolves to bool, valid
            "output:\n  album_folder_template: '{__class__}'\n",
            "download:\n  segment_concurrency: 3\ndownload:\n  segment_concurrency: 4\n",
        ):
            if text.startswith("output:\n  save_cover: yes"):
                continue
            with self.subTest(text=text):
                self._write(text)
                with self.assertRaises(ConfigError):
                    load_config(self.path)
                self.assertEqual(self.path.read_text(), text)

    def test_obsolete_profiles_are_dropped_like_any_unknown_key(self):
        original = (
            "download:\n  segment_concurrency: 4\n"
            "profiles:\n  fast:\n    download:\n      concurrent_downloads: 4\n"
        )
        self._write(original)
        cli = CliRunner().invoke(
            main, ["--config", str(self.path), "config", "validate"]
        )
        self.assertEqual(cli.exit_code, 0, cli.output)
        rewritten = yaml.safe_load(self.path.read_text())
        self.assertNotIn("profiles", rewritten)
        self.assertEqual(rewritten["download"]["segment_concurrency"], 4)
        self.assertEqual(self.path.with_name("config.yaml.bak").read_text(), original)
        self.assertEqual(load_config(self.path).download.segment_concurrency, 4)
        self.assertEqual(load_config(None).download.concurrent_downloads, 2)

        # Valid settings remain valid even when the discarded section has
        # settings that would be invalid under the current schema.
        self._write(
            "download:\n  segment_concurrency: 6\n"
            "profiles:\n  unused:\n    download:\n      concurrency: invalid\n"
        )
        self.assertEqual(load_config(self.path).download.segment_concurrency, 6)
        self.assertNotIn("profiles", yaml.safe_load(self.path.read_text()))

        result = CliRunner().invoke(
            main, ["--no-config", "--profile", "old", "config", "show"]
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("No such option", result.output)

    def test_validation_rejects_explicitly_missing_file(self):
        result = CliRunner().invoke(
            main, ["--config", str(self.path), "config", "validate"]
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("does not exist", result.output)

    def test_legacy_defaults_and_missing_prd_are_valid_for_interactive_setup(self):
        config = load_config(None)
        self.assertFalse(config.output.no_lyrics_file)
        self.assertFalse(config.output.save_playlist)
        self.assertEqual(config.download.song_codec_priority, ["aac-web"])
        self._write("auth:\n  drm_backend: playready\n")
        self.assertEqual(load_config(self.path).auth.drm_backend, "playready")

    def test_invalid_click_option_uses_same_error_style(self):
        result = CliRunner().invoke(
            main, ["--no-config", "rip", "URL", "--song-codec", "invalid"]
        )
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
                    "alac-hires",
                ],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--ask-song-codec conflicts", result.output)
