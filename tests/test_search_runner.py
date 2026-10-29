"""Search discovery sends precisely selected items across media types to download."""

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from gamdl.cli.commands.runner import run_rip
from gamdl.cli.options import RipOptions
from gamdl.download import DownloadResult, MediaStatus
from gamdl.media import SongCodec


class SearchRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, *, media_type=None, lucky=False):
        api = Mock(active_subscription=True, storefront="br")
        queries = []
        next_calls = []
        downloaded = []
        seen_caches = []

        async def get_search(term, *, types, limit):
            queries.append((term, types, limit))
            resources = {
                "songs": [
                    {"id": "s1", "type": "songs", "attributes": {"name": "Song 1"}},
                    {"id": "s2", "type": "songs", "attributes": {"name": "Song 2"}},
                ],
                "music-videos": [
                    {
                        "id": "v1",
                        "type": "music-videos",
                        "attributes": {"name": "Video 1"},
                    },
                ],
                "uploaded-videos": [
                    {
                        "id": "u1",
                        "type": "uploaded-videos",
                        "attributes": {"name": "Uploaded 1"},
                    },
                ],
            }
            return {"results": {types: {"data": resources[types]}}}

        async def get_next(url):
            next_calls.append(url)
            raise AssertionError("Search should not fetch extra pages")

        async def select(caches, term, *, media_type):
            seen_caches.append(caches)
            self.assertEqual(term, "example")
            self.assertEqual(media_type, "songs" if media_type else None)
            # Simulate the user opening some categories, never all automatically.
            await caches["songs"].get(0)
            if media_type:
                return {"songs": (1,)}
            await caches["music-videos"].get(0)
            await caches["uploaded-videos"].get(0)
            return {
                "songs": (1,),
                "music-videos": (0,),
                "uploaded-videos": (0,),
            }

        async def download(media, **kwargs):
            downloaded.append((media.id, media.type))
            return DownloadResult(
                media.id, Path("test.m4a"), MediaStatus.DOWNLOADED, 123, SongCodec.AAC
            )

        api.get_search_results = AsyncMock(side_effect=get_search)
        api.get_extended_api_data = AsyncMock(side_effect=get_next)
        downloader = Mock(itunes_api=Mock())
        downloader.download = AsyncMock(side_effect=download)
        options = RipOptions(urls=("search",), no_progress=True)
        options.search_term = "example"
        options.search_type = media_type
        options.feeling_lucky = lucky

        with (
            patch(
                "gamdl.cli.commands.runner._authenticate",
                new_callable=AsyncMock,
                return_value=(api, None),
            ),
            patch(
                "gamdl.cli.commands.runner.create_drm",
                new_callable=AsyncMock,
                return_value=Mock(),
            ),
            patch("gamdl.cli.commands.runner.Downloader", return_value=downloader),
            patch(
                "gamdl.cli.commands.runner.select_search_results", side_effect=select
            ) as picker,
        ):
            code = await run_rip(options)

        return code, queries, next_calls, downloaded, seen_caches, picker

    async def test_downloads_explicit_selections_across_all_three_types(self):
        code, queries, next_calls, downloaded, seen_caches, picker = await self._run()
        self.assertEqual(code, 0)
        self.assertEqual(
            downloaded,
            [("s2", "songs"), ("v1", "music-videos"), ("u1", "uploaded-videos")],
        )
        self.assertEqual(
            [types for _, types, _ in queries],
            ["songs", "music-videos", "uploaded-videos"],
        )
        self.assertEqual(next_calls, [])
        self.assertEqual(len(seen_caches), 1)
        self.assertEqual(picker.call_count, 1)  # One continuous UI session.

    async def test_type_flag_only_queries_chosen_type(self):
        code, queries, _, downloaded, _, _ = await self._run(media_type="song")
        self.assertEqual(code, 0)
        self.assertEqual(downloaded, [("s2", "songs")])
        self.assertEqual([types for _, types, _ in queries], ["songs"])

    async def test_feeling_lucky_stays_noninteractive(self):
        code, queries, _, downloaded, _, picker = await self._run(
            media_type="song", lucky=True
        )
        self.assertEqual(code, 0)
        self.assertEqual(downloaded, [("s1", "songs")])
        self.assertEqual([types for _, types, _ in queries], ["songs"])
        picker.assert_not_called()
