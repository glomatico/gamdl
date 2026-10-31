"""Playlist occurrences carry lazy, shared context independent of the CLI."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from gamdl.download import Downloader, PathFormatter, PlaylistTags
from gamdl.media import MediaResolver, MediaSource, Playlist, Song


class PlaylistMetadataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = Mock()
        self.api.get_playlist = AsyncMock(
            return_value={
                "data": [{"attributes": {"name": "Favorites", "curatorName": "DJ"}}]
            }
        )
        self.api.get_library_playlist = AsyncMock(
            return_value={
                "data": [{"attributes": {"name": "Library Mix", "curatorName": "Me"}}]
            }
        )

        async def items(*args, **kwargs):
            for index, song_id in enumerate(["42", "42", "43"]):
                yield {
                    "id": song_id,
                    "type": "songs",
                    "attributes": {"name": f"Song {index}"},
                }

        self.api.iter_playlist_tracks = Mock(side_effect=items)

    async def test_resolver_shares_one_lazy_playlist_reference(self):
        media = [item async for item in MediaResolver(self.api).from_playlist("p.123")]
        self.assertEqual([item.source.index for item in media], [0, 1, 2])
        self.assertEqual([item.id for item in media], ["42", "42", "43"])
        self.assertIs(media[0].source.playlist, media[1].source.playlist)
        self.assertIs(media[1].source.playlist, media[2].source.playlist)
        self.api.get_playlist.assert_not_awaited()
        data = await asyncio.gather(
            *(item.source.playlist.get_metadata() for item in media)
        )
        self.assertTrue(all(record.title == "Favorites" for record in data))
        self.assertEqual(data[0].artist, "DJ")
        self.api.get_playlist.assert_awaited_once_with("p.123", limit_tracks=1)

    async def test_library_playlist_uses_library_metadata_endpoint(self):
        media = [
            item
            async for item in MediaResolver(self.api).from_playlist(
                "p.library", is_library=True
            )
        ]
        self.assertEqual(
            (await media[0].source.playlist.get_metadata()).title, "Library Mix"
        )
        self.api.get_library_playlist.assert_awaited_once_with(
            "p.library", include="catalog", limit=1
        )
        self.api.get_playlist.assert_not_awaited()

    async def test_downloader_builds_playlist_from_media_and_source_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloader = Downloader(path_formatter=PathFormatter(root))
            playlist = Playlist("p.123", api=self.api)

            async def update(index: int):
                song = Song.from_id(str(index), api=self.api)
                song.source = MediaSource("playlist_tracks", "p.123", index, playlist)
                output = root / f"song-{index}.m4a"
                output.write_bytes(f"song {index}".encode())
                return await downloader.download(song, output, save_playlist=True)

            results = await asyncio.gather(*(update(i) for i in range(8)))
            self.assertTrue(all(result.status == "skipped" for result in results))
            self.assertEqual(self.api.get_playlist.await_count, 1)
            entries = (
                (root / "Playlists" / "DJ" / "Favorites.m3u8").read_text().splitlines()
            )
            self.assertEqual(
                entries,
                [
                    "#EXTM3U",
                    "#PLAYLIST:Favorites",
                    "#Gamdl-PLAYLIST-ARTIST:DJ",
                    "#Gamdl-APPLE-MUSIC-ID:p.123",
                    *[f"../../song-{i}.m4a" for i in range(8)],
                ],
            )

    async def test_explicit_index_overrides_occurrence_and_does_not_refetch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloader = Downloader(path_formatter=PathFormatter(root))
            playlist = Playlist("p.123", api=self.api)
            song = Song.from_id("42", api=self.api)
            song.source = MediaSource("playlist_tracks", "p.123", 2, playlist)
            target = root / "track.m4a"
            target.write_bytes(b"existing")
            result = await downloader.download(
                song, target, save_playlist=True, playlist_index=4
            )
            self.assertEqual(result.status, "skipped")
            entries = (
                (root / "Playlists" / "DJ" / "Favorites.m3u8").read_text().splitlines()
            )
            self.assertEqual(entries[4:8], ["", "", "", ""])
            self.assertEqual(entries[8], "../../track.m4a")

    async def test_explicit_tags_remain_supported_without_playlist_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloader = Downloader(path_formatter=PathFormatter(root))
            song = Song.from_id("42", api=self.api)
            target = root / "track.m4a"
            target.write_bytes(b"existing")
            result = await downloader.download(
                song,
                target,
                save_playlist=True,
                playlist_tags=PlaylistTags(title="Manual", artist="Curator", track=1),
            )
            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.playlist_file.path.name, "Manual.m3u8")
            self.api.get_playlist.assert_not_awaited()
