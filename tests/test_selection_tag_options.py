"""CLI selection policy and tag exclusion behavior."""

import unittest
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from gamdl.cli.main import main
from gamdl.media import MediaResolver
from tests.test_media import fake_api, make_tracks


class SelectionOptionsTests(unittest.TestCase):
    def test_rip_select_overrides_configured_auto_select(self):
        captured = []

        async def run(options):
            captured.append(options)
            return 0

        with patch("gamdl.cli.commands.runner.run_rip", side_effect=run):
            result = CliRunner().invoke(
                main,
                [
                    "--no-config",
                    "rip",
                    "URL",
                    "--select",
                    "--auto-select",
                    "artist-main-albums",
                    "--no-tag",
                    "--exclude-tags",
                    "title_sort,cover",
                    "--exclude-tags",
                    "comment",
                ],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(captured[0].select)
        self.assertTrue(captured[0].no_tag)
        self.assertEqual(captured[0].exclude_tags, ("title_sort,cover", "comment"))

    def test_library_has_no_selection_policy_flags(self):
        result = CliRunner().invoke(main, ["--no-config", "library", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("--auto-select", result.output)
        self.assertNotIn("--select ", result.output)
        self.assertIn("--no-tag", result.output)
        self.assertIn("--exclude-tags", result.output)


class ResolverSelectTests(unittest.IsolatedAsyncioTestCase):
    async def test_select_overrides_default_album_auto_selection(self):
        api = fake_api()

        async def tracks(_id, *, is_library=False):
            for item in make_tracks("a", "b"):
                yield item

        api.iter_album_tracks = tracks
        selector = AsyncMock(return_value=[1])
        resolver = MediaResolver(api, select=True, selector=selector)
        self.assertEqual(
            [item.id async for item in resolver.from_album("album")], ["b"]
        )
        selector.assert_awaited_once()
