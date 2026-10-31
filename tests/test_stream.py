"""Offline stream contracts: discovery, codecs and explicit interactive choice."""

import asyncio
import base64
import unittest
from unittest.mock import AsyncMock, Mock

import httpx

from gamdl.media import (
    HlsSongStream,
    HlsType,
    SelectionRequiredError,
    Song,
    SongCodec,
    StreamManifestError,
    StreamNotAvailableError,
    StreamOrigin,
    StreamSource,
    StreamSourceSelectionRequest,
    StreamSelectionRequest,
    WebPlaybackStream,
)
from gamdl.media.song.stream import _default_master_url
from gamdl.media.stream import StreamTransport

BASE = "https://cdn.example.test/media/"
WEB_MASTER = BASE + "web-asset.m3u8"
WEB_HE = BASE + "he-asset.m3u8"
WRAPPER_MASTER = BASE + "P42_default.m3u8"
ASSET_MASTER = BASE + "P93_default.m3u8"
STANDARD_ASSET_MASTER = BASE + "P94_default.m3u8"
ENHANCED = (
    "#EXTM3U\n#EXT-X-VERSION:6\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=2600000,AVERAGE-BANDWIDTH=2100000,CODECS="alac",AUDIO="audio-alac-96-24"\n'
    "alac-hi.m3u8\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=1500000,AVERAGE-BANDWIDTH=1200000,CODECS="alac",AUDIO="audio-alac-48-24"\n'
    "alac-lo.m3u8\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=320000,AVERAGE-BANDWIDTH=256000,CODECS="mp4a.40.2",AUDIO="audio-stereo-256"\n'
    "aac.m3u8\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=800000,AVERAGE-BANDWIDTH=650000,CODECS="ec-3",AUDIO="audio-atmos-768"\n'
    "atmos.m3u8\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=210000,AVERAGE-BANDWIDTH=128000,CODECS="mp4a.40.2",AUDIO="audio-stereo-128"\n'
    "aac-lo.m3u8\n"
)
STANDARD = (
    "#EXTM3U\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=130000,AVERAGE-BANDWIDTH=112000,CODECS="mp4a.40.5"\n'
    "he.m3u8\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=320000,AVERAGE-BANDWIDTH=272000,CODECS="mp4a.40.2"\n'
    "aac.m3u8\n"
)
DRM_KEYS = (
    '#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",URI="data:text/plain;base64,'
    + base64.b64encode(b"pssh example").decode()
    + '"\n'
    '#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.microsoft.playready",URI="https://licenses.example.test/pr"\n'
    '#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.apple.streamingkeydelivery",URI="skd://itunes.apple.com/asset"\n'
)
MEDIA_PLAYLIST = (
    "#EXTM3U\n#EXT-X-VERSION:6\n"
    + DRM_KEYS
    + '#EXT-X-MAP:URI="init.mp4"\n'
    + "#EXTINF:5.25,\nseg0.m4s\n#EXTINF:4.00,\nseg1.m4s\n#EXT-X-ENDLIST\n"
)


def fake_api(*, web_master: str | None = None):
    api = Mock()
    api.get_song = AsyncMock(
        return_value={
            "data": [{"id": "111", "attributes": {"playParams": {"id": "111"}}}]
        }
    )
    api.get_webplayback = AsyncMock(
        return_value={
            "songList": [
                {
                    "songId": "111",
                    "assets": [
                        {"flavor": "28:ctrp256", "URL": WEB_MASTER},
                        {"flavor": "32:ctrp64", "URL": WEB_HE},
                        {"flavor": "unsupported", "URL": BASE + "unknown.m3u8"},
                    ],
                    **({"hls-playlist-url": web_master} if web_master else {}),
                }
            ]
        }
    )

    async def get_assets(_id, **kwargs):
        filename = (
            "P93_custom.m3u8"
            if kwargs.get("hls_profile", "enhancedHls") == "enhancedHls"
            else "P94_custom.m3u8"
        )
        return {"results": {"assets": [{"url": BASE + filename}]}}

    api.get_assets = AsyncMock(side_effect=get_assets)
    return api


def stream_client(*, master=ENHANCED, asset_master=None, standard_master=STANDARD):
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path, request.headers.get("range")))
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "P42_default.m3u8":
            return httpx.Response(200, text=master)
        if name == "P93_default.m3u8":
            return httpx.Response(200, text=asset_master or master)
        if name == "P94_default.m3u8":
            return httpx.Response(200, text=standard_master)
        if name in {
            "alac-hi.m3u8",
            "alac-lo.m3u8",
            "aac.m3u8",
            "aac-lo.m3u8",
            "atmos.m3u8",
            "he.m3u8",
            "web-asset.m3u8",
            "he-asset.m3u8",
        }:
            return httpx.Response(200, text=MEDIA_PLAYLIST)
        if name == "init.mp4":
            return httpx.Response(200, content=b"init bytes")
        return httpx.Response(404, text="private CDN info")

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), calls


class StreamDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_codec_does_not_fetch_hls_or_segments(self):
        api = fake_api()
        song = Song.from_id("111", api=api)
        async with stream_client()[0] as client:
            stream = await song.get_stream(codec=SongCodec.AAC_WEB, client=client)
            self.assertIsInstance(stream, WebPlaybackStream)
            self.assertEqual(stream.url, WEB_MASTER)
            self.assertEqual(stream.origin, StreamOrigin.WEBPLAYBACK)
            self.assertEqual(stream.bitrate, 256000)
            api.get_webplayback.assert_awaited_once_with("111", is_library=False)
            api.get_assets.assert_not_awaited()

    async def test_hls_selects_highest_average_bandwidth_and_preserves_regex(self):
        api = fake_api()
        song = Song.from_id("111", api=api)
        client, calls = stream_client()
        async with client:
            alac = await song.get_stream(codec=SongCodec.ALAC, client=client)
            aac = await song.get_stream(codec=SongCodec.AAC, client=client)
            atmos = await song.get_stream(codec=SongCodec.ATMOS, client=client)
            self.assertIsInstance(alac, HlsSongStream)
            self.assertEqual(alac.bitrate, 2100000)
            self.assertEqual(alac.url, BASE + "alac-hi.m3u8")
            self.assertEqual(aac.bitrate, 256000)
            self.assertEqual(atmos.codec_string, "ec-3")
            self.assertEqual(alac.origin, StreamOrigin.ASSET_ENHANCED_HLS)
            self.assertEqual(alac.hls_type, HlsType.ENHANCED)
            self.assertEqual(alac.source_api, StreamSource.ASSETS)
            self.assertEqual(
                [path for _, path, _ in calls], ["/media/P93_default.m3u8"]
            )
            api.get_webplayback.assert_not_awaited()
            api.get_assets.assert_awaited_once()

    async def test_wrapper_hls_preferred_without_assets_request(self):
        api = fake_api()
        wrapper = Mock()
        wrapper.get_playback = AsyncMock(
            return_value={"songList": [{"hls-playlist-url": BASE + "P42_custom.m3u8"}]}
        )
        song = Song.from_id("111", api=api)
        client, calls = stream_client()
        async with client:
            selected = await song.get_stream(
                codec=SongCodec.ALAC, client=client, wrapper_api=wrapper
            )
            self.assertEqual(selected.origin, StreamOrigin.WRAPPER_HLS)
            self.assertEqual(selected.url, BASE + "alac-hi.m3u8")
            wrapper.get_playback.assert_awaited_once_with("111")
            api.get_assets.assert_not_awaited()
            self.assertEqual(calls[0][1], "/media/P42_default.m3u8")

    async def test_wrapper_no_requested_codec_tries_asset_fallback(self):
        api = fake_api()
        wrapper = Mock()
        wrapper.get_playback = AsyncMock(
            return_value={"songList": [{"hls-playlist-url": BASE + "P42_custom.m3u8"}]}
        )
        song = Song.from_id("111", api=api)
        client, calls = stream_client(master=STANDARD, asset_master=ENHANCED)
        async with client:
            selected = await song.get_stream(
                codec=SongCodec.ALAC, client=client, wrapper_api=wrapper
            )
            self.assertEqual(selected.origin, StreamOrigin.ASSET_ENHANCED_HLS)
            self.assertEqual(selected.codec, SongCodec.ALAC)
            self.assertIn("/media/P42_default.m3u8", [p for _, p, _ in calls])
            self.assertIn("/media/P93_default.m3u8", [p for _, p, _ in calls])

    async def test_missing_wrapper_hls_falls_back_to_assets_enhanced_only(self):
        api = fake_api()
        wrapper = Mock()
        wrapper.get_playback = AsyncMock(return_value={"songList": [{"assets": []}]})
        song = Song.from_id("111", api=api)
        client, calls = stream_client()
        async with client:
            selected = await song.get_stream(
                codec=SongCodec.ALAC, client=client, wrapper_api=wrapper
            )
            self.assertEqual(selected.origin, StreamOrigin.ASSET_ENHANCED_HLS)
            self.assertEqual(selected.codec, SongCodec.ALAC)
            api.get_webplayback.assert_not_awaited()
            api.get_assets.assert_awaited_once()
            self.assertEqual([p for _, p, _ in calls], ["/media/P93_default.m3u8"])

    async def test_no_standard_profile_request_for_missing_nonweb_codec(self):
        api = fake_api()
        wrapper = Mock()
        wrapper.get_playback = AsyncMock(
            return_value={"songList": [{"hls-playlist-url": BASE + "P42_custom.m3u8"}]}
        )
        song = Song.from_id("111", api=api)
        client, calls = stream_client(master=STANDARD, asset_master=STANDARD)
        async with client:
            with self.assertRaises(StreamNotAvailableError):
                await song.get_stream(
                    codec=SongCodec.ATMOS, client=client, wrapper_api=wrapper
                )
            self.assertEqual(
                [p for _, p, _ in calls],
                ["/media/P42_default.m3u8", "/media/P93_default.m3u8"],
            )
            api.get_assets.assert_awaited_once()

    async def test_wrapper_precedes_assets_even_if_assets_higher_bitrate(self):
        api = fake_api()
        wrapper = Mock()
        wrapper.get_playback = AsyncMock(
            return_value={"songList": [{"hls-playlist-url": BASE + "P42_custom.m3u8"}]}
        )
        song = Song.from_id("111", api=api)
        client, calls = stream_client(master=STANDARD, asset_master=ENHANCED)
        async with client:
            stream = await song.get_stream(
                codec=SongCodec.AAC, client=client, wrapper_api=wrapper
            )
            self.assertEqual(stream.origin, StreamOrigin.WRAPPER_HLS)
            self.assertEqual(stream.bitrate, 272000)
            self.assertEqual(stream.hls_type, HlsType.STANDARD)
            self.assertEqual([p for _, p, _ in calls], ["/media/P42_default.m3u8"])
            api.get_assets.assert_not_awaited()

    async def test_standard_hls_matches_codecs_without_audio_group(self):
        api = fake_api()
        song = Song.from_id("111", api=api)
        client, _ = stream_client(master=STANDARD, asset_master=STANDARD)
        async with client:
            he = await song.get_stream(codec=SongCodec.AAC_HE, client=client)
            self.assertEqual(he.url, BASE + "he.m3u8")
            self.assertEqual(he.hls_type, HlsType.STANDARD)
            self.assertEqual(he.origin, StreamOrigin.ASSET_ENHANCED_HLS)
            with self.assertRaises(StreamNotAvailableError):
                await song.get_stream(codec=SongCodec.ALAC, client=client)
            api.get_assets.assert_awaited_once_with(
                "111",
                kind="song",
                include_license_urls=True,
                hls_encryption="CBC",
                hls_profile="enhancedHls",
            )

    async def test_codec_priority_uses_first_available_codec(self):
        api = fake_api()
        song = Song.from_id("111", api=api)
        client, _ = stream_client()
        async with client:
            chosen = await song.get_stream(
                codec_priority=[SongCodec.AC3, SongCodec.ALAC, SongCodec.AAC_WEB],
                client=client,
            )
            self.assertEqual(chosen.codec, SongCodec.ALAC)
            web = await song.get_stream(
                codec_priority=[SongCodec.AAC_WEB, SongCodec.ALAC], client=client
            )
            self.assertEqual(web.codec, SongCodec.AAC_WEB)

    async def test_ask_selects_source_before_discovering_its_variants(self):
        api = fake_api()
        song = Song.from_id("111", api=api)
        seen = []

        async def choose_assets_enhanced(request):
            seen.append(request)
            if isinstance(request, StreamSourceSelectionRequest):
                api.get_assets.assert_not_awaited()
                api.get_webplayback.assert_not_awaited()
                return next(
                    o.index
                    for o in request.options
                    if o.origin == StreamOrigin.ASSET_ENHANCED_HLS
                )
            self.assertIsInstance(request, StreamSelectionRequest)
            return next(o.index for o in request.options if o.codec == SongCodec.ALAC)

        client, calls = stream_client()
        async with client:
            chosen = await song.get_stream(
                ask=True, selector=choose_assets_enhanced, client=client
            )
            self.assertEqual(chosen.codec, SongCodec.ALAC)
            self.assertEqual(seen[0].media_id, "111")
            self.assertEqual(len(seen), 2)
            self.assertEqual(
                [o.origin for o in seen[0].options],
                [
                    StreamOrigin.WEBPLAYBACK,
                    StreamOrigin.ASSET_ENHANCED_HLS,
                    StreamOrigin.ASSET_STANDARD_HLS,
                ],
            )
            self.assertEqual(seen[1].origin, StreamOrigin.ASSET_ENHANCED_HLS)
            self.assertTrue(
                all(
                    o.origin == StreamOrigin.ASSET_ENHANCED_HLS for o in seen[1].options
                )
            )
            api.get_webplayback.assert_not_awaited()
            self.assertEqual([x for x in calls if x[1].endswith("alac-hi.m3u8")], [])
            self.assertEqual([p for _, p, _ in calls], ["/media/P93_default.m3u8"])
            api.get_assets.assert_awaited_once()

    async def test_ask_webplayback_avoids_assets_and_wrapper(self):
        api = fake_api()
        wrapper = Mock()
        wrapper.get_playback = AsyncMock()
        song = Song.from_id("111", api=api)
        prompts = []

        def choose(request):
            prompts.append(request)
            if isinstance(request, StreamSourceSelectionRequest):
                self.assertEqual(
                    [o.origin for o in request.options],
                    [
                        StreamOrigin.WEBPLAYBACK,
                        StreamOrigin.WRAPPER_HLS,
                        StreamOrigin.ASSET_ENHANCED_HLS,
                        StreamOrigin.ASSET_STANDARD_HLS,
                    ],
                )
                return 0
            return 1

        client, calls = stream_client()
        async with client:
            result = await song.get_stream(
                ask=True, selector=choose, wrapper_api=wrapper, client=client
            )
            self.assertEqual(result.codec, SongCodec.AAC_HE_WEB)
            self.assertEqual(result.origin, StreamOrigin.WEBPLAYBACK)
            self.assertEqual(len(prompts), 2)
            wrapper.get_playback.assert_not_awaited()
            api.get_assets.assert_not_awaited()
            api.get_webplayback.assert_awaited_once()
            self.assertEqual(calls, [])

    async def test_ask_explicit_standard_profile_and_exact_origin(self):
        api = fake_api()
        song = Song.from_id("111", api=api)
        seen = []

        def choose(request):
            seen.append(request)
            if isinstance(request, StreamSourceSelectionRequest):
                return next(
                    o.index
                    for o in request.options
                    if o.origin == StreamOrigin.ASSET_STANDARD_HLS
                )
            return next(o.index for o in request.options if o.codec == SongCodec.AAC_HE)

        client, calls = stream_client()
        async with client:
            stream = await song.get_stream(ask=True, selector=choose, client=client)
            self.assertEqual(stream.origin, StreamOrigin.ASSET_STANDARD_HLS)
            self.assertEqual(stream.source_api, StreamSource.ASSETS)
            self.assertEqual(stream.hls_type, HlsType.STANDARD)
            self.assertEqual(stream.codec, SongCodec.AAC_HE)
            self.assertEqual([p for _, p, _ in calls], ["/media/P94_default.m3u8"])
            api.get_assets.assert_awaited_once_with(
                "111",
                kind="song",
                include_license_urls=True,
                hls_encryption="CBC",
                hls_profile="",
            )
            api.get_webplayback.assert_not_awaited()

    async def test_ask_wrapper_source_does_not_contact_other_apis(self):
        api = fake_api()
        wrapper = Mock()
        wrapper.get_playback = AsyncMock(
            return_value={"songList": [{"hls-playlist-url": BASE + "P42_custom.m3u8"}]}
        )
        song = Song.from_id("111", api=api)

        def select(request):
            if isinstance(request, StreamSourceSelectionRequest):
                return next(
                    o.index
                    for o in request.options
                    if o.origin == StreamOrigin.WRAPPER_HLS
                )
            return next(o.index for o in request.options if o.codec == SongCodec.ALAC)

        client, calls = stream_client()
        async with client:
            result = await song.get_stream(
                ask=True, selector=select, wrapper_api=wrapper, client=client
            )
            self.assertEqual(result.origin, StreamOrigin.WRAPPER_HLS)
            self.assertEqual(result.hls_type, HlsType.ENHANCED)
            self.assertEqual(result.source_api, StreamSource.WRAPPER)
            self.assertEqual([p for _, p, _ in calls], ["/media/P42_default.m3u8"])
            api.get_assets.assert_not_awaited()
            api.get_webplayback.assert_not_awaited()

    async def test_library_ask_uses_catalog_sources_instead_of_encrypted_library_hls(
        self,
    ):
        api = fake_api()
        song = Song.from_metadata(
            {
                "id": "i.123",
                "type": "library-songs",
                "relationships": {"catalog": {"data": [{"id": "111"}]}},
            },
            api=api,
        )
        prompts = []

        def select(request):
            prompts.append(request)
            return 0

        client, calls = stream_client()
        async with client:
            stream = await song.get_stream(ask=True, selector=select, client=client)
            self.assertEqual(stream.origin, StreamOrigin.WEBPLAYBACK)
            self.assertEqual(
                [o.origin for o in prompts[0].options],
                [
                    StreamOrigin.WEBPLAYBACK,
                    StreamOrigin.ASSET_ENHANCED_HLS,
                    StreamOrigin.ASSET_STANDARD_HLS,
                ],
            )
            self.assertEqual(len(prompts), 2)
            self.assertEqual(calls, [])
            api.get_assets.assert_not_awaited()
            api.get_webplayback.assert_awaited_once_with("111", is_library=False)

    async def test_full_discovery_preserves_all_distinct_origins(self):
        api = fake_api()
        wrapper = Mock()
        wrapper.get_playback = AsyncMock(
            return_value={"songList": [{"hls-playlist-url": BASE + "P42_custom.m3u8"}]}
        )
        song = Song.from_id("111", api=api)
        client, calls = stream_client()
        async with client:
            streams = await song.get_streams(client=client, wrapper_api=wrapper)
            self.assertEqual(
                {s.origin for s in streams},
                {
                    StreamOrigin.WEBPLAYBACK,
                    StreamOrigin.WRAPPER_HLS,
                    StreamOrigin.ASSET_ENHANCED_HLS,
                    StreamOrigin.ASSET_STANDARD_HLS,
                },
            )
            self.assertIn(
                StreamOrigin.ASSET_STANDARD_HLS,
                {s.origin for s in streams if s.codec == SongCodec.AAC},
            )
            self.assertEqual(
                [p for _, p, _ in calls],
                [
                    "/media/P42_default.m3u8",
                    "/media/P93_default.m3u8",
                    "/media/P94_default.m3u8",
                ],
            )
            self.assertEqual(api.get_assets.await_count, 2)

    async def test_ask_without_selector_raises_before_network(self):
        api = fake_api()
        song = Song.from_id("111", api=api)
        with self.assertRaises(SelectionRequiredError):
            await song.get_stream(ask=True)
        api.get_assets.assert_not_awaited()
        api.get_webplayback.assert_not_awaited()

    async def test_selector_validation_and_mutually_exclusive_codec_params(self):
        api = fake_api()
        song = Song.from_id("111", api=api)
        with self.assertRaises(ValueError):
            await song.get_stream(codec=SongCodec.ALAC, codec_priority=[SongCodec.AAC])
        with self.assertRaises(ValueError):
            await song.get_stream(codec_priority=[])
        client, _ = stream_client()
        async with client:
            with self.assertRaises(ValueError):
                await song.get_stream(ask=True, selector=lambda _: True, client=client)
            with self.assertRaises(ValueError):
                await song.get_stream(ask=True, selector=lambda _: 777, client=client)
            with self.assertRaises(ValueError):
                await song.get_stream(
                    ask=True,
                    selector=lambda request: (
                        0 if isinstance(request, StreamSourceSelectionRequest) else 99
                    ),
                    client=client,
                )

    async def test_ask_is_exclusive_of_codec_preferences(self):
        api = fake_api()
        song = Song.from_id("111", api=api)
        callback = Mock(return_value=0)
        with self.assertRaisesRegex(ValueError, "ask cannot be combined"):
            await song.get_stream(
                codec_priority=[SongCodec.AAC_WEB], ask=True, selector=callback
            )
        with self.assertRaisesRegex(ValueError, "ask cannot be combined"):
            await song.get_stream(codec=SongCodec.AAC_WEB, ask=True, selector=callback)
        with self.assertRaisesRegex(ValueError, "selector requires ask"):
            await song.get_stream(selector=callback)
        with self.assertRaises(ValueError):
            SongCodec("ask")
        callback.assert_not_called()

    async def test_library_catalog_web_stream_keeps_drm_and_catalog_media_id(self):
        api = fake_api()
        song = Song.from_metadata(
            {
                "id": "i.123",
                "type": "library-songs",
                "relationships": {"catalog": {"data": [{"id": "111"}]}},
            },
            api=api,
        )
        client, calls = stream_client()
        async with client:
            stream = await song.get_stream(codec=SongCodec.AAC_WEB, client=client)
            self.assertFalse(stream.drm_free)
            self.assertEqual(stream.origin, StreamOrigin.WEBPLAYBACK)
            self.assertEqual(stream.media_id, "111")
            self.assertTrue((await stream.get_drm_info()).is_protected)
            self.assertEqual(stream.codec, SongCodec.AAC_WEB)
            self.assertEqual(stream.url, WEB_MASTER)
            all_streams = await song.get_streams(client=client)
            self.assertIn(
                StreamOrigin.ASSET_ENHANCED_HLS, {s.origin for s in all_streams}
            )
            self.assertTrue(all(s.media_id == "111" for s in all_streams))
            self.assertIn(("GET", "/media/web-asset.m3u8", None), calls)
            api.get_webplayback.assert_awaited_with("111", is_library=False)

    async def test_library_without_catalog_counterpart_fails_explicitly(self):
        api = fake_api()
        song = Song.from_metadata(
            {
                "id": "i.upload",
                "type": "library-songs",
                "relationships": {"catalog": {"data": []}},
            },
            api=api,
        )
        with self.assertRaisesRegex(StreamNotAvailableError, "no single direct asset"):
            await song.get_stream()
        api.get_webplayback.assert_awaited_once_with("i.upload", is_library=True)


class StreamLazyTests(unittest.IsolatedAsyncioTestCase):
    async def test_manifest_segments_and_drm_share_one_playlist_request(self):
        client, calls = stream_client()
        async with client:
            stream = WebPlaybackStream(
                codec=SongCodec.AAC_WEB,
                origin=StreamOrigin.WEBPLAYBACK,
                url=WEB_MASTER,
                transport=StreamTransport(client),
            )
            self.assertEqual(calls, [])
            manifest, segments, drm = await asyncio.gather(
                stream.get_manifest(), stream.get_segments(), stream.get_drm_info()
            )
            self.assertEqual(len(manifest.segments), 2)
            self.assertEqual(
                [s.url for s in segments], [BASE + "seg0.m4s", BASE + "seg1.m4s"]
            )
            self.assertEqual(segments[0].duration, 5.25)
            self.assertEqual(drm.widevine_pssh, b"pssh example")
            self.assertEqual(drm.playready_uri, "https://licenses.example.test/pr")
            self.assertEqual(drm.fairplay_streaming, "skd://itunes.apple.com/asset")
            self.assertEqual(drm.encryption_method, "SAMPLE-AES")
            self.assertTrue(drm.is_protected)
            self.assertEqual(len(calls), 1)
            self.assertEqual(await stream.get_drm_info(), drm)
            self.assertEqual(await stream.get_init_segment(), b"init bytes")
            self.assertEqual(len(calls), 2)

    async def test_drm_free_playlist_and_missing_init_segment(self):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, text="#EXTM3U\n#EXTINF:1,\na.mp4\n")
            )
        )
        async with client:
            stream = HlsSongStream(
                codec=SongCodec.AAC,
                origin=StreamOrigin.ASSET_ENHANCED_HLS,
                url=BASE + "clear.m3u8",
                transport=StreamTransport(client),
            )
            self.assertFalse((await stream.get_drm_info()).is_protected)
            self.assertIsNone(await stream.get_init_segment())

    async def test_invalid_playlist_and_unsafe_url_errors_are_sanitized(self):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(500, text="signedsecret=veryprivate")
            )
        )
        async with client:
            transport = StreamTransport(client)
            with self.assertRaises(StreamManifestError) as raised:
                await transport.get_manifest(
                    "https://cdn.example.test/token=private/stream.m3u8"
                )
            self.assertNotIn("private", str(raised.exception))
            with self.assertRaises(StreamManifestError):
                await transport.get_manifest("http://localhost/private.m3u8")

    async def test_cdn_requests_and_redirects_never_inherit_credentials(self):
        observed = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            if request.url.path == "/start.m3u8":
                return httpx.Response(
                    302, headers={"Location": "https://next.example.test/final.m3u8"}
                )
            return httpx.Response(200, content=b"#EXTM3U\n#EXT-X-ENDLIST\n")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer secret", "Cookie": "session=secret"},
            auth=httpx.BasicAuth("private", "secret"),
        ) as client:
            client.cookies.set("session", "redirect-secret", domain="next.example.test")
            transport = StreamTransport(client)
            value = await transport.get(
                "https://cdn.example.test/start.m3u8", byte_range="0-1023"
            )
            self.assertEqual(value, b"#EXTM3U\n#EXT-X-ENDLIST\n")
        self.assertEqual(len(observed), 2)
        for request in observed:
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("cookie", request.headers)
            self.assertEqual(request.headers["range"], "bytes=0-1023")

    async def test_cdn_redirects_validate_every_url_and_limit_hops(self):
        unsafe_redirects = (
            "http://cdn.example.test/insecure",
            "https://user:secret@cdn.example.test/hidden",
            "file:///etc/passwd",
        )
        for redirected_to in unsafe_redirects:
            seen = []

            def handler(request):
                seen.append(request)
                return httpx.Response(302, headers={"Location": redirected_to})

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                with self.subTest(redirected_to=redirected_to):
                    with self.assertRaises(StreamManifestError):
                        await StreamTransport(client).get(
                            "https://cdn.example.test/start.m3u8"
                        )
                    self.assertEqual(len(seen), 1)

        seen = []

        def infinite_redirect(request):
            seen.append(request)
            return httpx.Response(302, headers={"Location": "/start.m3u8"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(infinite_redirect)
        ) as client:
            with self.assertRaisesRegex(StreamManifestError, "Too many"):
                await StreamTransport(client).get("https://cdn.example.test/start.m3u8")
        self.assertEqual(len(seen), 11)

    async def test_cdn_failure_not_cached(self):
        count = 0

        def handler(_):
            nonlocal count
            count += 1
            return httpx.Response(503 if count == 1 else 200, text=MEDIA_PLAYLIST)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            stream = WebPlaybackStream(
                codec=SongCodec.AAC_WEB,
                origin=StreamOrigin.WEBPLAYBACK,
                url=WEB_MASTER,
                transport=StreamTransport(client),
            )
            with self.assertRaises(StreamManifestError):
                await stream.get_manifest()
            self.assertEqual(len((await stream.get_manifest()).segments), 2)
            self.assertEqual(count, 2)

    def test_default_master_url_normalization(self):
        self.assertEqual(
            _default_master_url(BASE + "P123_weird.m3u8?token=hi"),
            BASE + "P123_default.m3u8?token=hi",
        )

    async def test_untyped_webplayback_key_is_not_falsely_labeled_pssh(self):
        playlist = (
            "#EXTM3U\n"
            '#EXT-X-KEY:METHOD=ISO-23001-7,URI="data:;base64,cHNzaA=="\n'
            "#EXTINF:2,\nsegment.mp4\n"
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text=playlist))
        ) as client:
            stream = WebPlaybackStream(
                codec=SongCodec.AAC_WEB,
                origin=StreamOrigin.WEBPLAYBACK,
                url=WEB_MASTER,
                transport=StreamTransport(client),
            )
            info = await stream.get_drm_info()
            self.assertTrue(info.is_protected)
            self.assertEqual(info.untyped_key_data, b"pssh")
            self.assertIsNone(info.widevine_pssh)
            self.assertIsNone(info.playready_pssh)

    async def test_fairplay_placeholder_does_not_override_real_key_reference(self):
        playlist = (
            "#EXTM3U\n"
            '#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.apple.streamingkeydelivery",URI="skd://itunes.apple.com/actual"\n'
            '#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.apple.streamingkeydelivery",URI="skd://itunes.apple.com/P000000000/s1/e1"\n'
            "#EXTINF:2,\nsegment.mp4\n"
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text=playlist))
        ) as client:
            stream = HlsSongStream(
                codec=SongCodec.ALAC,
                origin=StreamOrigin.ASSET_ENHANCED_HLS,
                url=BASE + "file.m3u8",
                transport=StreamTransport(client),
            )
            self.assertEqual(
                (await stream.get_drm_info()).fairplay_streaming,
                "skd://itunes.apple.com/actual",
            )

    async def test_init_segment_respects_byte_range(self):
        playlist = (
            "#EXTM3U\n#EXT-X-VERSION:6\n"
            '#EXT-X-MAP:URI="init.mp4",BYTERANGE="4@12"\n'
            "#EXTINF:2,\nsegment.mp4\n"
        )
        requested_ranges = []

        def handler(request):
            if request.url.path.endswith(".m3u8"):
                return httpx.Response(200, text=playlist)
            requested_ranges.append(request.headers.get("range"))
            return httpx.Response(206, content=b"INIT")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            stream = HlsSongStream(
                codec=SongCodec.ALAC,
                origin=StreamOrigin.ASSET_ENHANCED_HLS,
                url=ASSET_MASTER,
                transport=StreamTransport(client),
            )
            self.assertEqual(await stream.get_init_segment(), b"INIT")
            self.assertEqual(await stream.get_init_segment(), b"INIT")
            self.assertEqual(requested_ranges, ["bytes=12-15"])


if __name__ == "__main__":
    unittest.main()
