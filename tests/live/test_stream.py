"""Opt-in read-only stream discovery and DRM signaling tests.

These request playlists, not audio segments, DRM licenses or decryption keys.
GAMDL_RUN_LIVE=1 uv run python -m unittest discover -s tests/live -p test_stream.py -v
"""

import os
import unittest

from gamdl.media import (
    HlsSongStream,
    HlsType,
    Song,
    SongCodec,
    StreamOrigin,
    StreamSelectionRequest,
    StreamSourceSelectionRequest,
    WebPlaybackStream,
)
from tests.live_auth import LiveSettings, open_live_session
from tests.live.song_fixture import (
    ENHANCED_CODECS,
    SONG_ID,
    STANDARD_CODECS,
    WEB_CODECS,
)


@unittest.skipUnless(
    os.environ.get("GAMDL_RUN_LIVE") == "1",
    "Opt-in only: set GAMDL_RUN_LIVE=1 to contact Apple Music",
)
class StreamLiveTests(unittest.IsolatedAsyncioTestCase):
    """Read-only codec and DRM metadata for the Brazilian New Rules fixture."""

    async def asyncSetUp(self):
        self.context = open_live_session(LiveSettings.from_env(), interval=0.75)
        self.session = await self.context.__aenter__()
        self.addAsyncCleanup(self.context.__aexit__, None, None, None)
        self.song = Song.from_id(SONG_ID, api=self.session.api)

    async def test_webplayback_stream_and_drm_metadata(self):
        stream = await self.song.get_stream(codec=SongCodec.AAC_WEB)
        self.assertIsInstance(stream, WebPlaybackStream)
        self.assertEqual(stream.origin, StreamOrigin.WEBPLAYBACK)
        self.assertGreater(len((await stream.get_manifest()).segments), 0)
        drm = await stream.get_drm_info()
        self.assertTrue(drm.is_protected)
        self.assertTrue(drm.untyped_key_data or drm.untyped_key_uri)

    async def test_asset_hls_without_wrapper(self):
        stream = await self.song.get_stream(codec=SongCodec.ALAC)
        self.assertIsInstance(stream, HlsSongStream)
        self.assertEqual(stream.origin, StreamOrigin.ASSET_ENHANCED_HLS)
        self.assertGreater(stream.bitrate, 0)

    async def test_explicit_standard_assets_hls(self):
        calls = []

        async def choose(request):
            calls.append(type(request))
            if isinstance(request, StreamSourceSelectionRequest):
                return next(
                    o.index
                    for o in request.options
                    if o.origin == StreamOrigin.ASSET_STANDARD_HLS
                )
            self.assertIsInstance(request, StreamSelectionRequest)
            return next(o.index for o in request.options if o.codec == SongCodec.AAC)

        stream = await self.song.get_stream(codec=SongCodec.ASK, selector=choose)
        self.assertEqual(stream.origin, StreamOrigin.ASSET_STANDARD_HLS)
        self.assertEqual(stream.hls_type, HlsType.STANDARD)
        self.assertEqual(calls, [StreamSourceSelectionRequest, StreamSelectionRequest])

    async def test_wrapper_hls_and_ask_selection(self):
        if self.session.wrapper is None:
            self.skipTest("Authenticated wrapper source required for wrapper HLS")
        wrapper = self.session.wrapper
        if wrapper is not None:
            selected = await self.song.get_stream(
                codec=SongCodec.ALAC, wrapper_api=wrapper
            )
            self.assertEqual(selected.origin, StreamOrigin.WRAPPER_HLS)
            self.assertGreater(len(await selected.get_segments()), 0)
            self.assertTrue((await selected.get_drm_info()).fairplay_streaming)

            async def choose(request):
                if isinstance(request, StreamSourceSelectionRequest):
                    return next(
                        option.index
                        for option in request.options
                        if option.origin == StreamOrigin.WRAPPER_HLS
                    )
                return next(
                    option.index
                    for option in request.options
                    if option.origin == StreamOrigin.WRAPPER_HLS
                    and option.codec == SongCodec.ALAC
                )

            asked = await self.song.get_stream(
                codec=SongCodec.ASK, selector=choose, wrapper_api=wrapper
            )
            self.assertIsInstance(asked, HlsSongStream)
            self.assertEqual(asked.codec, SongCodec.ALAC)

    async def test_codec_inventory_for_future_muxer_coverage(self):
        from gamdl.media import SongStreamResolver

        resolver = SongStreamResolver(self.song, wrapper_api=self.session.wrapper)
        web = await resolver.get_web_streams()
        self.assertTrue(set(WEB_CODECS) <= {s.codec for s in web})
        self.assertFalse(any(s.codec == SongCodec.AC3 for s in web))

        for origin, expected in (
            (StreamOrigin.ASSET_ENHANCED_HLS, ENHANCED_CODECS),
            (StreamOrigin.ASSET_STANDARD_HLS, STANDARD_CODECS),
        ):
            with self.subTest(origin=origin.value):
                renditions = await resolver.get_hls_streams(origin=origin)
                found = {stream.codec for stream in renditions}
                self.assertTrue(
                    set(expected) <= found,
                    f"Live codec fixture changed for {origin.value}: "
                    f"missing {set(expected) - found}",
                )
                self.assertNotIn(SongCodec.AC3, found)

        if self.session.wrapper is not None:
            wrapper_renditions = await resolver.get_hls_streams(
                origin=StreamOrigin.WRAPPER_HLS
            )
            found = {stream.codec for stream in wrapper_renditions}
            self.assertTrue(set(ENHANCED_CODECS) <= found)
            self.assertNotIn(SongCodec.AC3, found)


if __name__ == "__main__":
    unittest.main()
