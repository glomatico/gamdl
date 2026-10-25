"""Opt-in regression for music-video HLS carrying separate clear captions.

    GAMDL_RUN_LIVE_DECRYPT=1 uv run python -m unittest tests.live.test_video_captions -v

The sample deliberately checks successful fragment decryption and preservation
of the captions, not H.264 decode correctness (tracked separately).
"""

from __future__ import annotations

import os
import unittest

from gamdl.api import ItunesApi
from gamdl.drm import Drm
from gamdl.media import MusicVideo
from gamdl.stream import Streamer

from tests.live_auth import LiveSettings, open_live_session


@unittest.skipUnless(
    os.environ.get("GAMDL_RUN_LIVE_DECRYPT") == "1",
    "Live DRM tests require an explicit opt-in",
)
class VideoCaptionLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_encrypted_video_with_clear_closed_caption_track(self):
        async with open_live_session(LiveSettings.from_env(), interval=0.75) as session:
            async with await ItunesApi.create(storefront="br") as itunes:
                streams = await MusicVideo.from_id(
                    "1874387735", api=session.api
                ).get_streams(
                    max_height=360,
                    video_codec="h264",
                    itunes_api=itunes,
                    wrapper_api=session.wrapper,
                )
                source = streams.video
                self.assertIn(b"clcp", await source.get_init_segment())
                # The first HLS segment contains both video and caption trafs.
                playlist = await source.get_manifest()
                playlist.segments[:] = playlist.segments[:1]
                async with Streamer(
                    source,
                    drm=Drm.from_widevine(session.api),
                    wrapper_api=session.wrapper,
                ) as streamer:
                    self.assertEqual(streamer.decryption_method, "widevine")
                    data = await streamer.read()
                self.assertGreater(len(data), 10_000)
                self.assertIn(b"clcp", data)
                self.assertIn(b"free", data)


if __name__ == "__main__":
    unittest.main()
