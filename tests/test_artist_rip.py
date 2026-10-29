"""No-network coverage for GAMDL 4 artist selection and rip configuration."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from click.testing import CliRunner

from gamdl.cli.config.loader import ConfigError, load_config
from gamdl.cli.main import main
from gamdl.media import (
    ArtistBrowseRequest,
    ArtistBrowseResult,
    ArtistCategorySelection,
    MediaResolver,
    SelectionKind,
)
from gamdl.media.constants import DEFAULT_RIP_AUTO_SELECT, RIP_AUTO_SELECT_NAMES
from gamdl.media.exceptions import SelectionRequiredError
from gamdl.pagination import MediaPage


def resource(kind: str, media_id: str, title: str) -> dict:
    return {"id": media_id, "type": kind, "attributes": {"name": title}}


class ArtistResolverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = Mock()
        self.api.get_artist = AsyncMock(
            return_value={"data": [{"id": "artist", "attributes": {"name": "Janet"}}]}
        )
        self.calls = []

        async def view(_id, view):
            self.calls.append(("view", view))
            if view == "full-albums":
                yield resource("albums", "a1", "Main Album")
                yield resource("albums", "a2", "Other Album")
            elif view == "top-songs":
                yield resource("songs", "s1", "Song")
            elif view == "singles":
                yield resource("albums", "ep", "Single")

        async def relationship(_id, relationship):
            self.calls.append(("relationship", relationship))
            if relationship == "music-videos":
                yield resource("music-videos", "mv1", "Video")
            if relationship == "albums":
                yield resource("albums", "a1", "Main Album")

        async def album_tracks(album_id, *, is_library=False):
            self.calls.append(("album", album_id))
            yield resource("songs", album_id + "-track", "Track")

        self.api.iter_artist_view = Mock(side_effect=view)
        self.api.iter_artist_relationship = Mock(side_effect=relationship)
        self.api.iter_album_tracks = Mock(side_effect=album_tracks)

        async def page_view(_id, view_name, *, cursor=None, limit=25):
            items = [item async for item in view(_id, view_name)]
            offset = int(cursor or 0)
            portion = items[offset : offset + limit]
            return MediaPage(
                tuple(portion),
                str(offset + limit) if offset + limit < len(items) else None,
            )

        async def page_relationship(_id, relation, *, cursor=None, limit=25):
            items = [item async for item in relationship(_id, relation)]
            offset = int(cursor or 0)
            portion = items[offset : offset + limit]
            return MediaPage(
                tuple(portion),
                str(offset + limit) if offset + limit < len(items) else None,
            )

        self.api.get_artist_view_page = AsyncMock(side_effect=page_view)
        self.api.get_artist_relationship_page = AsyncMock(side_effect=page_relationship)

    async def test_multiple_automatic_artist_categories_stream_albums_and_videos(self):
        resolver = MediaResolver(
            self.api,
            auto_select=[
                SelectionKind.ALBUM_TRACKS,
                SelectionKind.ARTIST_MAIN_ALBUMS,
                SelectionKind.ARTIST_MUSIC_VIDEOS,
            ],
        )
        result = [m async for m in resolver.from_artist("artist")]
        self.assertEqual([m.id for m in result], ["a1-track", "a2-track", "mv1"])
        self.assertEqual(result[0].source.id, "a1")
        self.assertEqual(result[1].source.index, 0)
        self.assertEqual(result[2].source.kind, "artist-music-videos")
        self.assertEqual(
            self.calls,
            [
                ("view", "full-albums"),
                ("album", "a1"),
                ("album", "a2"),
                ("relationship", "music-videos"),
            ],
        )
        self.api.get_artist.assert_not_awaited()  # fully automatic mode stays lazy

    async def test_top_songs_and_singles(self):
        resolver = MediaResolver(
            self.api,
            auto_select=[
                SelectionKind.ALBUM_TRACKS,
                SelectionKind.ARTIST_SINGLES_EPS,
                SelectionKind.ARTIST_TOP_SONGS,
            ],
        )
        result = [item async for item in resolver.from_artist("artist")]
        self.assertEqual([item.id for item in result], ["ep-track", "s1"])
        self.assertEqual(result[-1].source.index, 0)

    async def test_manual_category_and_item_selection(self):
        requests = []

        async def select(request):
            requests.append(request)
            page = await request.load_page(SelectionKind.ARTIST_MAIN_ALBUMS, 0)
            self.assertEqual(
                [item.title for item in page.items], ["Main Album", "Other Album"]
            )
            return ArtistBrowseResult(
                (ArtistCategorySelection(SelectionKind.ARTIST_MAIN_ALBUMS, (1,)),)
            )

        resolver = MediaResolver(self.api, selector=select)
        media = [item async for item in resolver.from_artist("artist")]
        self.assertEqual([item.id for item in media], ["a2-track"])
        self.assertIsInstance(requests[0], ArtistBrowseRequest)
        self.assertEqual(requests[0].artist_name, "Janet")
        self.api.get_artist.assert_awaited_once_with(
            "artist", include="", views="", limit=1
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(self.calls, [("view", "full-albums"), ("album", "a2")])

    async def test_artist_without_category_or_selector_raises(self):
        resolver = MediaResolver(self.api)
        with self.assertRaises(SelectionRequiredError):
            _ = [item async for item in resolver.from_artist("artist")]
        self.api.get_artist.assert_not_awaited()

    async def test_artist_album_selection_does_not_prompt_for_tracks(self):
        requests = []

        async def select(request):
            requests.append(request.kind)
            return [0]

        resolver = MediaResolver(
            self.api, selector=select, auto_select=[SelectionKind.ARTIST_MAIN_ALBUMS]
        )
        media = [item async for item in resolver.from_artist("artist")]
        self.assertEqual(len(media), 2)
        self.assertEqual(requests, [])

    async def test_manual_top_songs_keep_original_occurrence_positions(self):
        async def songs(_id, _view):
            for i in range(3):
                yield resource("songs", str(i), f"Song {i}")

        self.api.iter_artist_view = Mock(side_effect=songs)
        self.api.get_artist_view_page = AsyncMock(
            side_effect=lambda *_a, **_k: MediaPage(
                tuple(resource("songs", str(i), f"Song {i}") for i in range(3))
            )
        )

        async def select(request):
            await request.load_page(SelectionKind.ARTIST_TOP_SONGS, 0)
            return ArtistBrowseResult(
                (ArtistCategorySelection(SelectionKind.ARTIST_TOP_SONGS, (2,)),)
            )

        resolver = MediaResolver(self.api, selector=select)
        tracks = [track async for track in resolver.from_artist("artist")]
        self.assertEqual([track.id for track in tracks], ["2"])
        self.assertEqual(tracks[0].source.index, 2)

    async def test_whole_category_selected_without_fetching_until_download(self):
        async def select(request):
            self.assertEqual(self.calls, [])
            return ArtistBrowseResult(
                (ArtistCategorySelection(SelectionKind.ARTIST_MAIN_ALBUMS),)
            )

        resolver = MediaResolver(self.api, selector=select)
        media = [item async for item in resolver.from_artist("artist")]
        self.assertEqual([item.id for item in media], ["a1-track", "a2-track"])
        self.assertEqual(
            self.calls,
            [
                ("view", "full-albums"),
                ("album", "a1"),
                ("album", "a2"),
            ],
        )

    async def test_opened_category_uses_cached_items_once(self):
        async def select(request):
            first = await request.load_page(SelectionKind.ARTIST_MAIN_ALBUMS, 0)
            second = await request.load_page(SelectionKind.ARTIST_MAIN_ALBUMS, 0)
            self.assertEqual(first, second)
            self.assertEqual(self.calls, [("view", "full-albums")])
            return ArtistBrowseResult(
                (ArtistCategorySelection(SelectionKind.ARTIST_MAIN_ALBUMS),)
            )

        resolver = MediaResolver(self.api, selector=select)
        self.assertEqual(
            [item.id async for item in resolver.from_artist("artist")],
            ["a1-track", "a2-track"],
        )
        self.assertEqual(self.calls.count(("view", "full-albums")), 1)

    async def test_paged_artist_item_selection_and_download_reuses_loaded_pages(self):
        seen = []

        async def fetch(_id, view, *, cursor=None, limit=25):
            seen.append(cursor)
            if cursor is None:
                return MediaPage(
                    (
                        resource("albums", "one", "First"),
                        resource("albums", "two", "Second"),
                    ),
                    "next",
                )
            return MediaPage((resource("albums", "three", "Third"),))

        self.api.get_artist_view_page = AsyncMock(side_effect=fetch)

        async def select(request):
            first = await request.load_page(SelectionKind.ARTIST_MAIN_ALBUMS, 0)
            self.assertEqual([i.index for i in first.items], [0, 1])
            second = await request.load_page(SelectionKind.ARTIST_MAIN_ALBUMS, 1)
            self.assertEqual([i.index for i in second.items], [2])
            await request.load_page(SelectionKind.ARTIST_MAIN_ALBUMS, 0)
            return ArtistBrowseResult(
                (ArtistCategorySelection(SelectionKind.ARTIST_MAIN_ALBUMS, (2,)),)
            )

        resolver = MediaResolver(self.api, selector=select)
        tracks = [m async for m in resolver.from_artist("artist")]
        self.assertEqual([m.id for m in tracks], ["three-track"])
        self.assertEqual(seen, [None, "next"])

    async def test_all_except_first_unloaded_artist_pages_stay_selected(self):
        seen = []

        async def fetch(_id, view, *, cursor=None, limit=25):
            seen.append(cursor)
            return (
                MediaPage((resource("music-videos", "mv0", "Skip"),), "next")
                if cursor is None
                else MediaPage((resource("music-videos", "mv1", "Keep"),))
            )

        self.api.get_artist_relationship_page = AsyncMock(side_effect=fetch)

        async def select(request):
            await request.load_page(SelectionKind.ARTIST_MUSIC_VIDEOS, 0)
            self.assertEqual(seen, [None])
            return ArtistBrowseResult(
                (
                    ArtistCategorySelection(
                        SelectionKind.ARTIST_MUSIC_VIDEOS, None, (0,)
                    ),
                )
            )

        resolver = MediaResolver(self.api, selector=select)
        self.assertEqual([m.id async for m in resolver.from_artist("artist")], ["mv1"])
        self.assertEqual(seen, [None, "next"])

    async def test_selecting_only_first_page_does_not_discover_later_pages(self):
        seen = []

        async def fetch(_id, _name, *, cursor=None, limit=25):
            seen.append(cursor)
            if cursor is not None:
                raise AssertionError("Unselected continuation must not be fetched")
            return MediaPage((resource("music-videos", "mv0", "Chosen"),), "next")

        self.api.get_artist_relationship_page = AsyncMock(side_effect=fetch)

        async def select(request):
            await request.load_page(SelectionKind.ARTIST_MUSIC_VIDEOS, 0)
            return ArtistBrowseResult(
                (ArtistCategorySelection(SelectionKind.ARTIST_MUSIC_VIDEOS, (0,)),)
            )

        resolver = MediaResolver(self.api, selector=select)
        self.assertEqual([m.id async for m in resolver.from_artist("artist")], ["mv0"])
        self.assertEqual(seen, [None])


class RipCliSettingsTests(unittest.TestCase):
    def test_multiple_urls_and_repeatable_auto_select(self):
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
                    "https://music.apple.com/br/song/a/1",
                    "https://music.apple.com/br/song/b/2",
                    "--auto-select",
                    "artist-main-albums",
                    "--auto-select",
                    "artist-music-videos",
                ],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(received[0].urls), 2)
        self.assertEqual(
            received[0].auto_select, ("artist-main-albums", "artist-music-videos")
        )

    def test_default_auto_select_and_validated_yaml_override(self):
        self.assertEqual(
            tuple(load_config(None).download.rip_auto_select), DEFAULT_RIP_AUTO_SELECT
        )
        self.assertEqual(set(DEFAULT_RIP_AUTO_SELECT), set())
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.yaml"
            path.write_text(
                "download:\n  rip_auto_select: [albums-tracks, artist-main-albums]\n"
            )
            self.assertEqual(
                load_config(path).download.rip_auto_select,
                ["artist-main-albums"],
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
                        str(path),
                        "rip",
                        "https://music.apple.com/br/song/a/1",
                    ],
                )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(captured[0].auto_select, ("artist-main-albums",))
            for bad in ("unknown-category", "album-tracks"):
                path.write_text(f"download:\n  rip_auto_select: [{bad}]\n")
                with self.assertRaisesRegex(ConfigError, "rip_auto_select"):
                    load_config(path)

    def test_no_urls_and_invalid_auto_select_are_usage_errors(self):
        missing = CliRunner().invoke(main, ["--no-config", "rip"])
        self.assertNotEqual(missing.exit_code, 0)
        self.assertIn("Provide at least one Apple Music URL", missing.output)
        invalid = CliRunner().invoke(
            main, ["--no-config", "rip", "URL", "--auto-select", "invalid"]
        )
        self.assertNotEqual(invalid.exit_code, 0)
        self.assertIn("Invalid value for '--auto-select'", invalid.output)
