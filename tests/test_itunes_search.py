"""Public iTunes catalog search and provider-independent browser integration."""

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx

from gamdl.api import GamdlApiResponseError, ItunesApi
from gamdl.cli.commands.runner import run_rip
from gamdl.cli.options import RipOptions
from gamdl.download import DownloadResult, MediaStatus
from gamdl.media import SongCodec
from gamdl.search import create_search_caches, normalize_itunes_results


def sample(kind, track_id, name):
    return {
        "kind": kind,
        "trackId": track_id,
        "trackName": name,
        "artistName": "Sample Artist",
    }


class ItunesSearchApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_search_uses_country_and_no_storefront_lookup(self):
        visits = []

        def handler(request):
            visits.append(request)
            return httpx.Response(200, json={"resultCount": 1, "results": []})

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
            await ItunesApi.create(
                storefront="br", client=http, search_only=True
            ) as itunes,
        ):
            self.assertIsNone(itunes.storefront_id)
            await itunes.get_search_results("Caravan Palace", entity="song")
            await itunes.get_search_results("Caravan Palace", entity="musicVideo")
            with self.assertRaisesRegex(ValueError, "storefront ID"):
                await itunes.get_itunes_page("song", "123")

        self.assertEqual(len(visits), 2)
        self.assertTrue(all(r.url.path == "/search" for r in visits))
        self.assertEqual(visits[0].url.params["country"], "br")
        self.assertEqual(visits[0].url.params["term"], "Caravan Palace")
        self.assertEqual(visits[0].url.params["entity"], "song")
        self.assertEqual(visits[0].url.params["media"], "music")
        self.assertEqual(visits[1].url.params["media"], "musicVideo")
        self.assertEqual(visits[1].url.params["entity"], "musicVideo")
        self.assertEqual(visits[0].url.params["limit"], "200")

    async def test_rejects_unsupported_requests_and_malformed_responses(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"resultCount": 1, "results": {}})

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
            await ItunesApi.create(client=http, search_only=True) as itunes,
        ):
            for kwargs in (
                {"entity": "playlist"},
                {"limit": 0},
                {"limit": 201},
            ):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    await itunes.get_search_results("Test", **kwargs)
            with self.assertRaises(ValueError):
                await itunes.get_search_results(" ")
            with self.assertRaisesRegex(
                GamdlApiResponseError, "Invalid iTunes search response"
            ):
                await itunes.get_search_results("Test")
        self.assertEqual(len(calls), 1)


class ItunesSearchAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalize_drops_invalid_and_wrong_media_type(self):
        data = {
            "results": [
                sample("song", 123, "Test Song"),
                sample("music-video", 456, "Wrong Type"),
                sample("song", True, "Invalid ID"),
                sample("song", 789, ""),
                {"kind": "song", "trackName": "No ID"},
                None,
            ]
        }
        results = normalize_itunes_results(data, "songs")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "123")
        self.assertEqual(results[0]["type"], "songs")
        self.assertEqual(results[0]["attributes"]["name"], "Test Song")
        self.assertEqual(results[0]["_search_api"], "itunes")

    async def test_one_itunes_request_per_category_for_four_local_pages(self):
        itunes = Mock()
        itunes.get_search_results = AsyncMock(
            side_effect=lambda term, *, entity, limit: {
                "results": [
                    sample(
                        "song" if entity == "song" else "music-video", i, f"Item {i}"
                    )
                    for i in range(125)
                ]
            }
        )
        api = Mock()
        caches = create_search_caches(
            api,
            "Example",
            ("songs", "music-videos"),
            search_api="itunes",
            itunes_api=itunes,
        )
        for kind in ("songs", "music-videos"):
            self.assertEqual(len((await caches[kind].get(0)).items), 50)
            self.assertEqual(len((await caches[kind].get(1)).items), 50)
            self.assertEqual(len((await caches[kind].get(2)).items), 25)
            self.assertIsNone(await caches[kind].get(3))
        self.assertEqual(itunes.get_search_results.await_count, 2)
        self.assertEqual(
            [c.kwargs["entity"] for c in itunes.get_search_results.await_args_list],
            ["song", "musicVideo"],
        )
        api.get_search_results.assert_not_called()

    async def test_itunes_rejects_unsupported_category(self):
        with self.assertRaisesRegex(ValueError, "only supports"):
            create_search_caches(
                Mock(),
                "Test",
                ("uploaded-videos",),
                search_api="itunes",
                itunes_api=Mock(),
            )

    async def test_selected_itunes_record_is_not_used_as_download_metadata(self):
        from gamdl.media import Media

        entry = normalize_itunes_results(
            {"results": [sample("song", 1228739609, "From iTunes")]}, "songs"
        )[0]
        api = Mock()
        api.get_song = AsyncMock(
            return_value={
                "data": [
                    {
                        "id": "1228739609",
                        "type": "songs",
                        "attributes": {"name": "From Apple Music"},
                    }
                ]
            }
        )
        media = Media.from_id(entry["id"], media_type=entry["type"], api=api)
        self.assertEqual(
            (await media.get_metadata())["attributes"]["name"], "From Apple Music"
        )
        api.get_song.assert_awaited_once()


class ItunesRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_lucky_search_downloads_itunes_selected_catalog_id(self):
        api = Mock(active_subscription=True, storefront="br")
        api.get_song = AsyncMock(
            return_value={
                "data": [
                    {
                        "id": "1228739609",
                        "type": "songs",
                        "attributes": {"name": "Authoritative Apple Music title"},
                    }
                ]
            }
        )
        itunes = MagicMock()
        itunes.__aenter__.return_value = itunes
        itunes.get_search_results = AsyncMock(
            return_value={"results": [sample("song", 1228739609, "Test Song")]}
        )
        downloaded = []

        async def download(media, **_kwargs):
            downloaded.append((media.id, media.type))
            return DownloadResult(
                media.id, Path("test.m4a"), MediaStatus.DOWNLOADED, 5, SongCodec.AAC
            )

        downloader = Mock()
        downloader.download = AsyncMock(side_effect=download)
        options = RipOptions(urls=("search",), no_progress=True)
        options.search_term = "Example"
        options.search_type = "song"
        options.search_api = "itunes"
        options.feeling_lucky = True
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
                "gamdl.cli.commands.runner.ItunesApi.create",
                new_callable=AsyncMock,
                return_value=itunes,
            ) as create,
        ):
            result = await run_rip(options)
        self.assertEqual(result, 0)
        self.assertEqual(downloaded, [("1228739609", "songs")])
        api.get_song.assert_awaited_once_with("1228739609")
        create.assert_awaited_once_with(storefront="br", search_only=True)
        itunes.__aexit__.assert_awaited_once()
