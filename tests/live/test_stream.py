"""Opt-in read-only stream discovery and DRM signaling tests.

These request playlists, not audio segments, DRM licenses or decryption keys.
GAMDL_RUN_LIVE=1 uv run python -m unittest discover -s tests/live -p test_stream.py -v
"""

import os
import unittest

from gamdl.api import WrapperApi
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

SONG_ID = "1559885421"


@unittest.skipUnless(
    os.environ.get("GAMDL_RUN_LIVE") == "1",
    "Opt-in only: set GAMDL_RUN_LIVE=1 to contact Apple Music",
)
class StreamLiveTests(unittest.IsolatedAsyncioTestCase):
    """Read-only streaming metadata of a BR Rick Astley song."""

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


if __name__ == "__main__":
    unittest.main()
