"""Lazy personal-library browsing and the library CLI command."""

import unittest
from unittest.mock import AsyncMock, Mock, patch

from click.testing import CliRunner

from gamdl.cli.main import main
from gamdl.media import MediaResolver, SelectionKind
from gamdl.media.models import (
    ArtistBrowseResult,
    ArtistCategorySelection,
    LibraryBrowseRequest,
)

SONGS = SelectionKind.LIBRARY_SONGS
ALBUMS = SelectionKind.LIBRARY_ALBUMS
PLAYLISTS = SelectionKind.LIBRARY_PLAYLISTS
VIDEOS = SelectionKind.LIBRARY_MUSIC_VIDEOS


def resource(media_type, media_id, name):
    return {"type": media_type, "id": media_id, "attributes": {"name": name}}


class LibraryResolverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = Mock()
        self.api.get_library_songs = AsyncMock(
            return_value={"data": [resource("library-songs", "i.song", "Song")]}
        )
        self.api.get_library_music_videos = AsyncMock(
            return_value={
                "data": [resource("library-music-videos", "i.video", "Video")]
            }
        )
        self.api.get_library_albums = AsyncMock(
            return_value={"data": [resource("library-albums", "i.album", "Album")]}
        )
        self.api.get_library_playlists = AsyncMock(
            return_value={
                "data": [resource("library-playlists", "i.playlist", "Playlist")]
            }
        )

        async def album_tracks(album_id, *, is_library=False):
            self.assertEqual(album_id, "i.album")
            self.assertTrue(is_library)
            yield resource("library-songs", "i.album-song", "Album Song")

        async def playlist_tracks(playlist_id, *, is_library=False):
            self.assertEqual(playlist_id, "i.playlist")
            self.assertTrue(is_library)
            yield resource("library-songs", "i.dup", "Duplicate")
            yield resource("library-songs", "i.dup", "Duplicate")

        self.api.iter_album_tracks = Mock(side_effect=album_tracks)
        self.api.iter_playlist_tracks = Mock(side_effect=playlist_tracks)

    async def test_selection_expands_all_four_categories_and_preserves_duplicates(self):
        async def choose(request):
            self.assertIsInstance(request, LibraryBrowseRequest)
            self.assertEqual(request.artist_name, "Your Library")
            self.assertEqual(len(request.categories), 4)
            self.api.get_library_songs.assert_not_awaited()
            albums = await request.load_page(ALBUMS, 0)
            self.assertEqual(albums.items[0].title, "Album")
            return ArtistBrowseResult(
                (
                    ArtistCategorySelection(PLAYLISTS),
                    ArtistCategorySelection(ALBUMS, (0,)),
                    ArtistCategorySelection(SONGS),
                    ArtistCategorySelection(VIDEOS),
                )
            )

        resolver = MediaResolver(self.api, selector=choose)
        items = [item async for item in resolver.from_library()]
        self.assertEqual(
            [item.id for item in items],
            ["i.dup", "i.dup", "i.album-song", "i.song", "i.video"],
        )
        self.assertTrue(all(item.is_library for item in items))
        self.assertIsNot(items[0], items[1])
        self.assertEqual(items[0].source.playlist.id, "i.playlist")
        self.assertEqual(items[-1].source.kind, VIDEOS.value)
        self.api.get_library_albums.assert_awaited_once_with(limit=100, offset=0)

    async def test_cursor_pages_and_individual_selection_do_not_fetch_extra_pages(self):
        all_songs = [
            resource("library-songs", f"i.{i}", f"Song {i}") for i in range(101)
        ]

        async def songs(*, limit, offset):
            self.assertEqual(limit, 100)
            return {
                "data": all_songs[offset : offset + limit],
                "next": "opaque server link" if offset == 0 else None,
            }

        self.api.get_library_songs.side_effect = songs

        async def choose(request):
            self.api.get_library_songs.assert_not_awaited()
            first = await request.load_page(SONGS, 0)
            second = await request.load_page(SONGS, 1)
            self.assertEqual((len(first.items), second.items[0].index), (100, 100))
            return ArtistBrowseResult((ArtistCategorySelection(SONGS, (100,)),))

        items = [
            item
            async for item in MediaResolver(self.api, selector=choose).from_library()
        ]
        self.assertEqual([item.id for item in items], ["i.100"])
        self.assertEqual(self.api.get_library_songs.await_count, 2)

    async def test_select_all_except_one_is_lazy_and_preserves_unseen_pages(self):
        all_songs = [
            resource("library-songs", f"i.{i}", f"Song {i}") for i in range(101)
        ]

        async def songs(*, limit, offset):
            return {
                "data": all_songs[offset : offset + limit],
                "next": "/page2" if offset == 0 else None,
            }

        self.api.get_library_songs.side_effect = songs

        async def choose(request):
            first = await request.load_page(SONGS, 0)
            self.assertEqual(first.items[0].index, 0)
            return ArtistBrowseResult((ArtistCategorySelection(SONGS, None, (0,)),))

        items = [
            item
            async for item in MediaResolver(self.api, selector=choose).from_library()
        ]
        self.assertEqual(len(items), 100)
        self.assertEqual(items[0].id, "i.1")
        self.assertEqual(items[-1].id, "i.100")
        self.assertEqual(self.api.get_library_songs.await_count, 2)

    async def test_invalid_selection_is_rejected_before_download(self):
        def choose(request):
            return ArtistBrowseResult((ArtistCategorySelection(SONGS, (0,)),))

        with self.assertRaisesRegex(ValueError, "invalid item"):
            _ = [
                item
                async for item in MediaResolver(
                    self.api, selector=choose
                ).from_library()
            ]
        self.api.get_library_songs.assert_not_awaited()

    async def test_library_url_resolves_to_browser(self):
        async def choose(request):
            return ArtistBrowseResult((ArtistCategorySelection(VIDEOS),))

        resolver = MediaResolver(self.api, selector=choose)
        items = [
            item async for item in resolver.from_url("https://music.apple.com/library")
        ]
        self.assertEqual([item.id for item in items], ["i.video"])


class LibraryCommandTests(unittest.TestCase):
    def test_help_exposes_library_command_without_url_argument(self):
        runner = CliRunner()
        root = runner.invoke(main, ["--no-config", "--help"])
        self.assertEqual(root.exit_code, 0, root.output)
        self.assertIn("library", root.output)
        help_result = runner.invoke(main, ["--no-config", "library", "--help"])
        self.assertEqual(help_result.exit_code, 0, help_result.output)
        self.assertIn("library [OPTIONS]", help_result.output)
        self.assertNotIn("[URLS]", help_result.output)

    def test_command_reuses_rip_options_and_implicit_library_url(self):
        with (
            patch("gamdl.cli.commands.library.require_terminal"),
            patch(
                "gamdl.cli.commands.runner.run_rip",
                new_callable=AsyncMock,
                return_value=0,
            ) as run,
        ):
            result = CliRunner().invoke(
                main,
                [
                    "--no-config",
                    "library",
                    "--output-dir",
                    "Test Library",
                    "--no-progress",
                ],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        opts = run.await_args.args[0]
        self.assertEqual(opts.urls, ("https://music.apple.com/library",))
        self.assertEqual(opts.output_dir, "Test Library")
        self.assertFalse(opts.progress)
