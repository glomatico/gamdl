"""Music-video HLS source discovery and independent audio/video descriptors."""

import unittest
from unittest.mock import AsyncMock, Mock

import httpx

from gamdl.api.exceptions import GamdlApiResponseError
from gamdl.cli.tui.selector import stream_choices
from gamdl.media import (
    AudioSelectionRequest,
    AudioStream,
    MusicVideo,
    MusicVideoCodec,
    SelectionRequiredError,
    StreamSource,
    VideoSelectionRequest,
    VideoStream,
)
from gamdl.media.exceptions import StreamNotAvailableError
from tests.test_media import VIDEO_DATA, fake_api

MASTER = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio-stereo",NAME="English",LANGUAGE="en",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio-stereo",NAME="Español",LANGUAGE="es",DEFAULT=NO,URI="audio-es.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio-atmos",NAME="Spatial",DEFAULT=YES,URI="atmos.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="unrelated",NAME="Other",DEFAULT=YES,URI="unrelated.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=3500000,CODECS="avc1.640028,mp4a.40.2",RESOLUTION=1920x1080,AUDIO="audio-stereo",FRAME-RATE=29.970
v1080.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1800000,CODECS="avc1.4d401f,mp4a.40.2",RESOLUTION=1280x720,AUDIO="audio-stereo"
v720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,CODECS="hvc1.1.6.L123,ec-3",RESOLUTION=3840x2160,AUDIO="audio-atmos"
v4k.m3u8
"""
AUDIO_PLAYLISTS = {
    "/audio.m3u8": """#EXTM3U
#EXT-X-BITRATE:256
#EXTINF:6,
audio1.m4s
""",
    "/audio-es.m3u8": """#EXTM3U
#EXTINF:6,
#EXT-X-BYTERANGE:96000@0
audio-es.m4s
#EXTINF:6,
#EXT-X-BYTERANGE:96000@96000
audio-es.m4s
""",
    "/atmos.m3u8": """#EXTM3U
#EXTINF:6,
#EXT-X-BYTERANGE:576000@0
atmos.m4s
""",
}
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
            if request.url.path in AUDIO_PLAYLISTS:
                return httpx.Response(200, text=AUDIO_PLAYLISTS[request.url.path])
            return httpx.Response(404)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        await self.client.__aenter__()
        self.addAsyncCleanup(self.client.__aexit__, None, None, None)
        self.api = video_api()
        self.itunes = Mock()
        self.itunes.get_itunes_page = AsyncMock(return_value=PAGE)
        self.media = MusicVideo.from_id("299199411", api=self.api)

    async def test_wrapper_does_not_supply_video_hls_and_itunes_is_used(self):
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
            itunes_api=self.itunes,
            max_height=720,
        )
        self.assertIsInstance(streams.video, VideoStream)
        self.assertIsInstance(streams.audio, AudioStream)
        self.assertEqual((streams.video.width, streams.video.height), (1280, 720))
        self.assertEqual(streams.video.codec, "h264")
        self.assertIs(streams.video.codec, MusicVideoCodec.H264)
        self.assertEqual(streams.video.url, "https://cdn.example.com/v720.m3u8")
        self.assertEqual(streams.audio.url, "https://cdn.example.com/audio.m3u8")
        self.assertEqual(streams.video.frame_rate, None)
        self.assertIs(
            await self.media.get_streams(
                client=self.client,
                wrapper_api=wrapper,
                itunes_api=self.itunes,
                max_height=720,
            ),
            streams,
        )
        wrapper.get_playback.assert_not_awaited()
        self.itunes.get_itunes_page.assert_awaited_once_with("music-video", "299199411")

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

    async def test_wrapper_alone_is_not_a_video_hls_source(self):
        wrapper = Mock()
        wrapper.get_playback = AsyncMock(
            return_value={
                "songList": [
                    {"hls-playlist-url": "https://invalid.example/wrapper.m3u8"}
                ]
            }
        )
        self.api.get_webplayback = AsyncMock(
            return_value={
                "songList": [
                    {"hls-playlist-url": "https://cdn.example.com/master.m3u8"}
                ]
            }
        )
        streams = await self.media.get_streams(
            client=self.client, wrapper_api=wrapper, max_height=720
        )
        self.assertEqual(streams.video.origin, "webplayback")
        wrapper.get_playback.assert_not_awaited()
        self.api.get_webplayback.assert_awaited_once_with("299199411", is_library=False)

    async def test_itunes_without_hls_falls_back_to_direct_webplayback(self):
        self.itunes.get_itunes_page.return_value = {
            "storePlatformData": {
                "product-dv": {"results": {"299199411": {"offers": []}}}
            }
        }
        self.api.get_webplayback = AsyncMock(
            return_value={
                "songList": [
                    {"hls-playlist-url": "https://cdn.example.com/master.m3u8"}
                ]
            }
        )
        streams = await self.media.get_streams(
            client=self.client, itunes_api=self.itunes, max_height=720
        )
        self.assertEqual(streams.video.origin, "webplayback")
        self.itunes.get_itunes_page.assert_awaited_once()
        self.api.get_webplayback.assert_awaited_once()

    async def test_itunes_api_error_falls_back_to_direct_webplayback(self):
        self.itunes.get_itunes_page.side_effect = GamdlApiResponseError(
            "missing", status_code=404
        )
        self.api.get_webplayback = AsyncMock(
            return_value={
                "songList": [
                    {"hls-playlist-url": "https://cdn.example.com/master.m3u8"}
                ]
            }
        )
        streams = await self.media.get_streams(
            client=self.client, itunes_api=self.itunes
        )
        self.assertEqual(streams.video.origin, "webplayback")

    async def test_mismatched_canonical_video_id_prefers_webplayback(self):
        self.api.get_music_video.return_value = {
            "data": [
                {
                    "id": "299199411",
                    "type": "music-videos",
                    "attributes": {
                        "url": "https://music.apple.com/us/music-video/alternate/7654321",
                        "playParams": {"id": "299199411", "kind": "musicVideo"},
                    },
                }
            ]
        }
        self.api.get_webplayback = AsyncMock(
            return_value={
                "songList": [
                    {"hls-playlist-url": "https://cdn.example.com/master.m3u8"}
                ]
            }
        )
        streams = await self.media.get_streams(
            client=self.client, itunes_api=self.itunes
        )
        self.assertEqual(streams.video.origin, "webplayback")
        self.itunes.get_itunes_page.assert_not_awaited()

    async def test_no_public_hls_returns_specific_source_error(self):
        self.itunes.get_itunes_page.return_value = {
            "storePlatformData": {"product-dv": {"results": {}}}
        }
        self.api.get_webplayback = AsyncMock(return_value={"songList": [{}]})
        with self.assertRaisesRegex(
            StreamNotAvailableError, "iTunes or Apple Music web playback"
        ):
            await self.media.get_streams(client=self.client, itunes_api=self.itunes)

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
            itunes_api=self.itunes,
            video_codec=MusicVideoCodec.H265,
            max_height=2160,
        )
        self.assertEqual(streams.video.width, 3840)
        with self.assertRaises(ValueError):
            await self.media.get_streams(
                client=self.client,
                wrapper_api=wrapper,
                itunes_api=self.itunes,
                video_codec="av1",
            )

    async def test_video_codec_priority_order_and_height(self):
        wrapper = Mock()
        wrapper.get_playback = AsyncMock(
            return_value={
                "songList": [
                    {"hls-playlist-url": "https://cdn.example.com/master.m3u8"}
                ]
            }
        )
        # Priorities outrank resolution: prefer 1080p AVC over 2160p HEVC.
        avc_first = await self.media.get_streams(
            client=self.client,
            wrapper_api=wrapper,
            itunes_api=self.itunes,
            max_height=None,
            codec_priority=[MusicVideoCodec.H264, MusicVideoCodec.H265],
        )
        self.assertEqual(
            (avc_first.video.codec, avc_first.video.height), ("h264", 1080)
        )
        hevc_first = await self.media.get_streams(
            client=self.client,
            wrapper_api=wrapper,
            itunes_api=self.itunes,
            max_height=None,
            codec_priority=[MusicVideoCodec.H265, MusicVideoCodec.H264],
        )
        self.assertEqual(
            (hevc_first.video.codec, hevc_first.video.height), ("h265", 2160)
        )
        # Even when the preferred video links an Atmos group, automatic
        # selection picks AAC from another variant in the same master.
        self.assertEqual(hevc_first.audio.url, "https://cdn.example.com/audio.m3u8")
        self.assertEqual(hevc_first.audio.codec.value, "aac")
        # Height is a hard limit even when HEVC is preferred.
        fallback = await self.media.get_streams(
            client=self.client,
            wrapper_api=wrapper,
            itunes_api=self.itunes,
            max_height=720,
            codec_priority=[MusicVideoCodec.H265, MusicVideoCodec.H264],
        )
        self.assertEqual((fallback.video.codec, fallback.video.height), ("h264", 720))
        # Identical normalized preference tuples reuse cached discovery results.
        calls_before = len(self.calls)
        self.assertIs(
            await self.media.get_streams(
                client=self.client,
                wrapper_api=wrapper,
                itunes_api=self.itunes,
                max_height=None,
                codec_priority=["h264", "h265"],
            ),
            avc_first,
        )
        self.assertEqual(len(self.calls), calls_before)

    async def test_priority_fallback_when_first_codec_missing(self):
        hevc_only = MASTER.replace('CODECS="avc1.', 'CODECS="unsupported.')
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text=hevc_only))
        ) as client:
            wrapper = Mock()
            wrapper.get_playback = AsyncMock(
                return_value={
                    "songList": [
                        {"hls-playlist-url": "https://cdn.example.com/master.m3u8"}
                    ]
                }
            )
            result = await self.media.get_streams(
                client=client,
                wrapper_api=wrapper,
                itunes_api=self.itunes,
                codec_priority=[MusicVideoCodec.H264, MusicVideoCodec.H265],
                max_height=None,
            )
            self.assertEqual(result.video.codec, MusicVideoCodec.H265)
            # Single codec remains an exact requirement, not a preference fallback.
            with self.assertRaises(StreamNotAvailableError):
                await self.media.get_streams(
                    client=client,
                    wrapper_api=wrapper,
                    itunes_api=self.itunes,
                    video_codec=MusicVideoCodec.H264,
                    max_height=None,
                )

    async def test_priority_without_aac_never_falls_back_to_atmos(self):
        video_with_orphan_audio = MASTER.replace(
            'RESOLUTION=1920x1080,AUDIO="audio-stereo"',
            'RESOLUTION=1920x1080,AUDIO="missing-group"',
        ).replace(
            'RESOLUTION=1280x720,AUDIO="audio-stereo"',
            'RESOLUTION=1280x720,AUDIO="missing-group"',
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, text=video_with_orphan_audio)
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
            with self.assertRaisesRegex(StreamNotAvailableError, "AAC or HE-AAC"):
                await self.media.get_streams(
                    client=client,
                    wrapper_api=wrapper,
                    itunes_api=self.itunes,
                    codec_priority=[MusicVideoCodec.H264, MusicVideoCodec.H265],
                    max_height=None,
                )

    async def test_auto_audio_highest_bitrate_aac_over_default_and_atmos(self):
        # The 128 kbps default AAC loses to the 320 kbps non-default AAC.
        playlist = {
            "/audio.m3u8": "#EXTM3U\n#EXT-X-BITRATE:128\n#EXTINF:6,\na.m4s\n",
            "/audio-es.m3u8": "#EXTM3U\n#EXT-X-BITRATE:320\n#EXTINF:6,\nb.m4s\n",
            "/atmos.m3u8": AUDIO_PLAYLISTS["/atmos.m3u8"],
        }
        calls = []

        def handle(request):
            path = request.url.path
            calls.append(path)
            if path == "/master.m3u8":
                return httpx.Response(200, text=MASTER)
            if path in playlist:
                return httpx.Response(200, text=playlist[path])
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            streams = await self.media.get_streams(
                client=client, itunes_api=self.itunes
            )
            self.assertEqual(streams.audio.url, "https://cdn.example.com/audio-es.m3u8")
            self.assertEqual(streams.audio.codec.value, "aac")
            self.assertEqual(streams.audio.bitrate, 320_000)
            self.assertNotIn("/atmos.m3u8", calls)
            self.assertNotIn("/audio1.m4s", calls)

    async def test_auto_audio_prefers_aac_over_higher_bitrate_he_aac(self):
        he_group = (
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio-he",NAME="HE",DEFAULT=YES,URI="he.m3u8"\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=3500000,CODECS="avc1.640028,mp4a.40.5",RESOLUTION=1920x1080,AUDIO="audio-he"\n'
            "he-video.m3u8\n"
        )
        master = MASTER + he_group

        def handle(request):
            if request.url.path == "/master.m3u8":
                return httpx.Response(200, text=master)
            if request.url.path == "/he.m3u8":
                return httpx.Response(
                    200, text="#EXTM3U\n#EXT-X-BITRATE:640\n#EXTINF:6,\na.m4s\n"
                )
            if request.url.path in AUDIO_PLAYLISTS:
                return httpx.Response(200, text=AUDIO_PLAYLISTS[request.url.path])
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            streams = await self.media.get_streams(
                client=client, itunes_api=self.itunes
            )
            self.assertEqual(streams.audio.codec.value, "aac")
            self.assertEqual(streams.audio.url, "https://cdn.example.com/audio.m3u8")

    async def test_auto_audio_falls_back_to_highest_bitrate_he_aac(self):
        he_only = MASTER.replace("mp4a.40.2", "mp4a.40.5")

        def handle(request):
            if request.url.path == "/master.m3u8":
                return httpx.Response(200, text=he_only)
            if request.url.path in AUDIO_PLAYLISTS:
                return httpx.Response(200, text=AUDIO_PLAYLISTS[request.url.path])
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            streams = await self.media.get_streams(
                client=client, itunes_api=self.itunes
            )
            self.assertEqual(streams.audio.codec.value, "aac-he")
            self.assertEqual(streams.audio.url, "https://cdn.example.com/audio.m3u8")
            self.assertEqual(streams.audio.bitrate, 256_000)

    async def test_auto_audio_rejects_only_surround_formats(self):
        surround = MASTER.replace("mp4a.40.2", "ac-3")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text=surround))
        ) as client:
            with self.assertRaisesRegex(StreamNotAvailableError, "AAC or HE-AAC"):
                await self.media.get_streams(client=client, itunes_api=self.itunes)

    async def test_auto_audio_missing_bitrate_uses_default_aac(self):
        # No segment hints or audio-only bandwidth information are available.
        def handle(request):
            if request.url.path == "/master.m3u8":
                return httpx.Response(200, text=MASTER)
            if request.url.path in ("/audio.m3u8", "/audio-es.m3u8"):
                return httpx.Response(200, text="#EXTM3U\n#EXTINF:6,\na.m4s\n")
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            streams = await self.media.get_streams(
                client=client, itunes_api=self.itunes
            )
            self.assertEqual(streams.audio.url, "https://cdn.example.com/audio.m3u8")
            self.assertIsNone(streams.audio.bitrate)

    async def test_priority_validation_before_network(self):
        for kwargs in (
            {"codec_priority": []},
            {"codec_priority": ["av1"]},
            {"codec_priority": "h264"},
            {
                "codec_priority": [MusicVideoCodec.H264],
                "video_codec": MusicVideoCodec.H265,
            },
            {
                "codec_priority": [MusicVideoCodec.H264],
                "ask": True,
                "selector": lambda _: 0,
            },
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await self.media.get_streams(**kwargs)
        self.api.get_music_video.assert_not_awaited()

    async def test_strict_max_height_and_unlimited(self):
        wrapper = Mock()
        wrapper.get_playback = AsyncMock(
            return_value={
                "songList": [
                    {"hls-playlist-url": "https://cdn.example.com/master.m3u8"}
                ]
            }
        )
        with self.assertRaises(StreamNotAvailableError):
            await self.media.get_streams(
                client=self.client,
                wrapper_api=wrapper,
                itunes_api=self.itunes,
                max_height=360,
            )
        with self.assertRaises(StreamNotAvailableError):
            await self.media.get_streams(
                client=self.client,
                wrapper_api=wrapper,
                itunes_api=self.itunes,
                video_codec=MusicVideoCodec.H265,
                max_height=1080,
            )
        streams = await self.media.get_streams(
            client=self.client,
            wrapper_api=wrapper,
            itunes_api=self.itunes,
            video_codec=MusicVideoCodec.H265,
            max_height=None,
        )
        self.assertEqual(streams.video.height, 2160)
        for invalid in (0, -1, True, 1080.0, "1080"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                await self.media.get_streams(max_height=invalid)

    async def test_interactive_video_and_independent_audio_selection(self):
        wrapper = Mock()
        wrapper.get_playback = AsyncMock(
            return_value={
                "songList": [
                    {"hls-playlist-url": "https://cdn.example.com/master.m3u8"}
                ]
            }
        )
        requests = []

        async def choose(request):
            requests.append(request)
            if isinstance(request, VideoSelectionRequest):
                self.assertEqual(
                    [o.codec for o in request.options],
                    [MusicVideoCodec.H264, MusicVideoCodec.H264, MusicVideoCodec.H265],
                )
                self.assertEqual([o.height for o in request.options], [1080, 720, 2160])
                self.assertAlmostEqual(request.options[0].frame_rate, 29.970)
                return 1
            self.assertIsInstance(request, AudioSelectionRequest)
            self.assertEqual(request.video.height, 720)
            self.assertEqual(
                [o.name for o in request.options], ["English", "Español", "Spatial"]
            )
            self.assertEqual([o.language for o in request.options], ["en", "es", None])
            self.assertEqual(
                [o.group_id for o in request.options],
                ["audio-stereo", "audio-stereo", "audio-atmos"],
            )
            self.assertEqual(request.options[2].codec.value, "atmos")
            self.assertEqual(
                [o.bitrate for o in request.options],
                [256_000, 128_000, 768_000],
            )
            title, choices = stream_choices(request)
            self.assertEqual(title, "Music-video audio")
            self.assertEqual(
                [choice.detail for choice in choices],
                [
                    "English · en · 256 kbps",
                    "Español · es · 128 kbps",
                    "Spatial · 768 kbps",
                ],
            )
            return 2

        streams = await self.media.get_streams(
            client=self.client,
            wrapper_api=wrapper,
            itunes_api=self.itunes,
            max_height=None,
            ask=True,
            selector=choose,
        )
        self.assertEqual(len(requests), 2)
        self.assertEqual(streams.video.url, "https://cdn.example.com/v720.m3u8")
        self.assertEqual(streams.audio.url, "https://cdn.example.com/atmos.m3u8")
        self.assertEqual(streams.audio.codec.value, "atmos")
        # Choices aren't cached: callbacks must run for each interactive call.
        await self.media.get_streams(
            client=self.client,
            wrapper_api=wrapper,
            itunes_api=self.itunes,
            max_height=None,
            ask=True,
            selector=choose,
        )
        self.assertEqual(len(requests), 4)

    async def test_audio_only_variant_bandwidth_is_used_without_media_probe(self):
        master = MASTER + (
            "#EXT-X-STREAM-INF:BANDWIDTH=320000,AVERAGE-BANDWIDTH=256000,"
            'CODECS="mp4a.40.2",AUDIO="audio-stereo"\n'
            "audio.m3u8\n"
        )
        calls = []

        def handle(request):
            calls.append(request.url.path)
            if request.url.path == "/master.m3u8":
                return httpx.Response(200, text=master)
            if request.url.path in AUDIO_PLAYLISTS:
                return httpx.Response(200, text=AUDIO_PLAYLISTS[request.url.path])
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:

            def choose(request):
                if isinstance(request, VideoSelectionRequest):
                    self.assertEqual(len(request.options), 3)
                    return 0
                self.assertEqual(request.options[0].bitrate, 256_000)
                return 0

            streams = await self.media.get_streams(
                client=client,
                itunes_api=self.itunes,
                max_height=None,
                ask=True,
                selector=choose,
            )
        self.assertEqual(streams.audio.bitrate, 256_000)
        self.assertNotIn("/audio.m3u8", calls)

    async def test_missing_audio_bitrate_does_not_block_selection(self):
        # No EXT-X-BITRATE, byte ranges or audio-only variants are available.
        def handle(request):
            if request.url.path == "/master.m3u8":
                return httpx.Response(200, text=MASTER)
            if request.url.path == "/audio.m3u8":
                return httpx.Response(200, text="#EXTM3U\n#EXTINF:6,\npart.m4s\n")
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:

            def choose(request):
                if isinstance(request, VideoSelectionRequest):
                    return 0
                self.assertEqual([o.bitrate for o in request.options], [None] * 3)
                return 0

            streams = await self.media.get_streams(
                client=client,
                itunes_api=self.itunes,
                ask=True,
                selector=choose,
            )
        self.assertIsNone(streams.audio.bitrate)
        self.assertEqual(streams.audio.url, "https://cdn.example.com/audio.m3u8")

    async def test_interactive_limits_and_selector_validation(self):
        wrapper = Mock()
        wrapper.get_playback = AsyncMock(
            return_value={
                "songList": [
                    {"hls-playlist-url": "https://cdn.example.com/master.m3u8"}
                ]
            }
        )
        with self.assertRaises(SelectionRequiredError):
            await self.media.get_streams(ask=True)
        with self.assertRaisesRegex(ValueError, "ask cannot be combined"):
            await self.media.get_streams(
                ask=True, video_codec=MusicVideoCodec.H264, selector=lambda _: 0
            )
        with self.assertRaisesRegex(ValueError, "selector requires ask"):
            await self.media.get_streams(selector=lambda _: 0)
        for bad in (True, -1, 30):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                await self.media.get_streams(
                    client=self.client,
                    wrapper_api=wrapper,
                    itunes_api=self.itunes,
                    ask=True,
                    selector=lambda _, bad=bad: bad,
                )
        with self.assertRaises(ValueError):
            await self.media.get_streams(
                client=self.client,
                wrapper_api=wrapper,
                itunes_api=self.itunes,
                ask=True,
                selector=lambda request: (
                    0 if isinstance(request, VideoSelectionRequest) else 12
                ),
            )
        with self.assertRaises(StreamNotAvailableError):
            await self.media.get_streams(
                client=self.client,
                wrapper_api=wrapper,
                itunes_api=self.itunes,
                max_height=360,
                ask=True,
                selector=lambda _: 0,
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
                await self.media.get_streams(
                    wrapper_api=wrapper, itunes_api=self.itunes, client=client
                )

    async def test_invalid_codec_and_no_source(self):
        with self.assertRaises(ValueError):
            await self.media.get_streams(video_codec="vp9")
        with self.assertRaises(StreamNotAvailableError):
            await self.media.get_streams()
