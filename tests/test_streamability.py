"""Regression coverage for Gamdl 3.x-style early catalog availability checks."""

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from gamdl.api.exceptions import GamdlApiResponseError
from gamdl.download import TagResolver
from gamdl.media import (
    MediaNotFoundError,
    MediaNotStreamableError,
    MusicVideo,
    Song,
    SongCodec,
)
from tests.test_media import PLAYBACK_TAG_DATA, SONG_DATA, VIDEO_DATA, fake_api


class StreamabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_unstreamable_song_short_circuits_wrapper_tags_and_streams(self):
        api = fake_api()
        wrapper = Mock(get_playback=AsyncMock())
        song = Song.from_metadata(
            {**SONG_DATA, "attributes": {"name": "Track 1"}}, api=api
        )

        with self.assertRaisesRegex(MediaNotStreamableError, "not streamable"):
            await TagResolver(wrapper_api=wrapper).get_tags(song, codec=SongCodec.AAC)
        with self.assertRaises(MediaNotStreamableError):
            await song.get_stream(codec=SongCodec.AAC, wrapper_api=wrapper)

        api.get_song.assert_not_awaited()  # Uses prefetched album-track metadata.
        api.get_webplayback.assert_not_awaited()
        api.get_assets.assert_not_awaited()
        wrapper.get_playback.assert_not_awaited()

    async def test_unstreamable_web_codec_skips_webplayback(self):
        api = fake_api()
        song = Song.from_metadata(
            {**SONG_DATA, "attributes": {"playParams": {}}}, api=api
        )
        with self.assertRaises(MediaNotStreamableError):
            await TagResolver().get_tags(song, codec=SongCodec.AAC_WEB)
        with self.assertRaises(MediaNotStreamableError):
            await song.get_stream(codec=SongCodec.AAC_WEB)
        api.get_webplayback.assert_not_awaited()

    async def test_catalog_404_becomes_unavailable_without_playback(self):
        api = fake_api()
        api.get_song.side_effect = GamdlApiResponseError("not found", status_code=404)
        song = Song.from_id("479349123", api=api)
        wrapper = Mock(get_playback=AsyncMock())
        with self.assertRaises(MediaNotFoundError):
            await TagResolver(wrapper_api=wrapper).get_tags(song)
        with self.assertRaises(MediaNotFoundError):
            await song.get_stream(codec=SongCodec.AAC, wrapper_api=wrapper)
        api.get_webplayback.assert_not_awaited()
        wrapper.get_playback.assert_not_awaited()

    async def test_unexpected_catalog_api_failure_is_not_masked(self):
        api = fake_api()
        api.get_song.side_effect = GamdlApiResponseError(
            "server error", status_code=500
        )
        song = Song.from_id("111", api=api)
        with self.assertRaises(GamdlApiResponseError) as context:
            await song.get_stream(codec=SongCodec.AAC)
        self.assertEqual(context.exception.status_code, 500)

    async def test_catalog_metadata_is_cached_across_tags_and_stream_checks(self):
        api = fake_api()
        wrapper = Mock(
            get_playback=AsyncMock(
                return_value={
                    "songList": [{"assets": [{"metadata": PLAYBACK_TAG_DATA}]}]
                }
            )
        )
        song = Song.from_id("1559885421", api=api)
        await asyncio.gather(song.ensure_streamable(), song.ensure_streamable())
        await TagResolver(wrapper_api=wrapper).get_tags(song, codec=SongCodec.AAC)
        await song.ensure_streamable()
        api.get_song.assert_awaited_once_with(song.id)
        wrapper.get_playback.assert_awaited_once_with(song.id)
        api.get_webplayback.assert_not_awaited()

    async def test_unstreamable_video_skips_wrapper_and_itunes(self):
        api = fake_api()
        video = MusicVideo.from_metadata(
            {**VIDEO_DATA, "attributes": {"name": "Unavailable"}}, api=api
        )
        wrapper = Mock(get_playback=AsyncMock())
        itunes = Mock(get_itunes_page=AsyncMock())
        with self.assertRaises(MediaNotStreamableError):
            await TagResolver(wrapper_api=wrapper, itunes_api=itunes).get_tags(video)
        with self.assertRaises(MediaNotStreamableError):
            await video.get_streams(wrapper_api=wrapper, itunes_api=itunes)
        wrapper.get_playback.assert_not_awaited()
        itunes.get_itunes_page.assert_not_awaited()
        api.get_music_video.assert_not_awaited()

    async def test_library_upload_does_not_require_catalog_playparams(self):
        api = fake_api()
        library = Song.from_metadata(
            {"id": "i.upload", "type": "library-songs", "attributes": {}},
            api=api,
        )
        api.get_library_song.return_value = {
            "data": [{"id": "i.upload", "relationships": {"catalog": {"data": []}}}]
        }
        await library.ensure_streamable()
        self.assertTrue(await library.is_uploaded())
        api.get_song.assert_not_awaited()

    async def test_library_catalog_equivalent_requires_playparams(self):
        api = fake_api()
        library = Song.from_metadata(
            {
                "id": "i.mapped",
                "type": "library-songs",
                "attributes": {},
                "relationships": {"catalog": {"data": [{"id": "111"}]}},
            },
            api=api,
        )
        api.get_song.return_value = {
            "data": [{"id": "111", "attributes": {"name": "Unavailable"}}]
        }
        with self.assertRaises(MediaNotStreamableError):
            await library.ensure_streamable()
        api.get_song.assert_awaited_once_with("111")
