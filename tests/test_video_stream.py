"""Music-video HLS source discovery and independent audio/video descriptors."""

import unittest
from unittest.mock import AsyncMock, Mock

import httpx

from gamdl.media import AudioStream, MusicVideo, VideoStream, StreamSource
from gamdl.media.exceptions import StreamNotAvailableError
from tests.test_media import VIDEO_DATA, fake_api

MASTER = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio-stereo",NAME="English",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=3500000,CODECS="avc1.640028,mp4a.40.2",RESOLUTION=1920x1080,AUDIO="audio-stereo",FRAME-RATE=29.970
v1080.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1800000,CODECS="avc1.4d401f,mp4a.40.2",RESOLUTION=1280x720,AUDIO="audio-stereo"
v720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,CODECS="hvc1.1.6.L123,mp4a.40.2",RESOLUTION=3840x2160,AUDIO="audio-stereo"
v4k.m3u8
"""
PAGE = {
    "storePlatformData": {
        "product-dv": {
            "results": {
                "299199411": {
                    "offers": [
                        {"assets": [{"hlsUrl": "https://cdn.example.com/master.m3u8"}]}
                    ]
                }
            }
        }
    }
}


def video_api():
    api = fake_api()
    api.get_music_video = AsyncMock(return_value={"data": [VIDEO_DATA]})
    return api


class VideoStreamDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []

        def handle(request):
            self.calls.append(str(request.url))
            if request.url.path == "/master.m3u8":
                return httpx.Response(200, text=MASTER)
            return httpx.Response(404)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        await self.client.__aenter__()
        self.addAsyncCleanup(self.client.__aexit__, None, None, None)
        self.api = video_api()
        self.media = MusicVideo.from_id("299199411", api=self.api)

    async def test_wrapper_source_and_quality_preference(self):
        wrapper = Mock()
        wrapper.get_playback = AsyncMock(
            return_value={
                "songList": [
                    {"hls-playlist-url": "https://cdn.example.com/master.m3u8"}
                ]
            }
        )
        streams = await self.media.get_streams(
            client=self.client, wrapper_api=wrapper, max_height=720
        )
        self.assertIsInstance(streams.video, VideoStream)
        self.assertIsInstance(streams.audio, AudioStream)
        self.assertEqual((streams.video.width, streams.video.height), (1280, 720))
        self.assertEqual(streams.video.codec, "h264")
        self.assertEqual(streams.video.url, "https://cdn.example.com/v720.m3u8")
        self.assertEqual(streams.audio.url, "https://cdn.example.com/audio.m3u8")
        self.assertEqual(streams.video.frame_rate, None)
        self.assertIs(
            await self.media.get_streams(
                client=self.client, wrapper_api=wrapper, max_height=720
            ),
            streams,
        )
        wrapper.get_playback.assert_awaited_once()
        self.api.get_music_video.assert_not_awaited()

    async def test_itunes_fallback_and_highest_resolution(self):
        itunes = Mock()
        itunes.get_itunes_page = AsyncMock(return_value=PAGE)
        itunes.storefront_id = 143441
        streams = await self.media.get_streams(
            client=self.client, itunes_api=itunes, max_height=2160, video_codec="h264"
        )
        self.assertEqual(streams.video.height, 1080)
        self.assertEqual(streams.video.source_api, StreamSource.ITUNES)
        self.assertAlmostEqual(streams.video.frame_rate, 29.970)
        self.assertEqual(streams.audio.url, "https://cdn.example.com/audio.m3u8")
        itunes.get_itunes_page.assert_awaited_once_with("music-video", "299199411")

    async def test_hevc_and_unsupported_codecs(self):
        wrapper = Mock()
        wrapper.get_playback = AsyncMock(
            return_value={
                "songList": [
                    {"hls-playlist-url": "https://cdn.example.com/master.m3u8"}
                ]
            }
        )
        streams = await self.media.get_streams(
            client=self.client,
            wrapper_api=wrapper,
            video_codec="h265",
            max_height=2160,
        )
        self.assertEqual(streams.video.width, 3840)
        with self.assertRaises(StreamNotAvailableError):
            await self.media.get_streams(
                client=self.client, wrapper_api=wrapper, video_codec="av1"
            )

    async def test_no_audio_rendition_rejected(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    text='#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000,CODECS="avc1.640028",RESOLUTION=640x360\nv.m3u8\n',
                )
            )
        ) as client:
            wrapper = Mock()
            wrapper.get_playback = AsyncMock(
                return_value={
                    "songList": [
                        {"hls-playlist-url": "https://cdn.example.com/master.m3u8"}
                    ]
                }
            )
            with self.assertRaisesRegex(StreamNotAvailableError, "separate audio"):
                await self.media.get_streams(wrapper_api=wrapper, client=client)

    async def test_invalid_codec_and_no_source(self):
        with self.assertRaises(ValueError):
            await self.media.get_streams(video_codec="vp9")
        with self.assertRaises(StreamNotAvailableError):
            await self.media.get_streams()
