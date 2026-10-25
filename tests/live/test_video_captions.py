"""Opt-in regression for music-video HLS carrying separate clear captions.

    GAMDL_RUN_LIVE_DECRYPT=1 uv run python -m unittest tests.live.test_video_captions -v

The short sample checks caption preservation. The full-video tests require
strict FFmpeg decoding, so CBCS pattern regressions cannot pass silently.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from gamdl.api import ItunesApi
from gamdl.drm import Drm
from gamdl.media import MusicVideo
from gamdl.muxer import Muxer
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

    @unittest.skipUnless(
        shutil.which("ffmpeg"), "FFmpeg is required for video validation"
    )
    async def test_complete_h264_video_strict_decoding(self):
        async with open_live_session(LiveSettings.from_env(), interval=0.75) as session:
            async with await ItunesApi.create(storefront="br") as itunes:
                for video_id, captions in (
                    ("299199411", False),
                    ("1874387735", True),
                ):
                    with self.subTest(video_id=video_id):
                        source = (
                            await MusicVideo.from_id(
                                video_id, api=session.api
                            ).get_streams(
                                max_height=360,
                                video_codec="h264",
                                itunes_api=itunes,
                                wrapper_api=session.wrapper,
                            )
                        ).video
                        if captions:
                            self.assertIn(b"clcp", await source.get_init_segment())
                        with TemporaryDirectory() as directory:
                            output = Path(directory) / "decoded.m4v"
                            async with Streamer(
                                source,
                                drm=Drm.from_widevine(session.api),
                                wrapper_api=session.wrapper,
                            ) as streamer:
                                self.assertEqual(streamer.decryption_method, "widevine")
                                await streamer.save(output)
                            self.assertGreater(output.stat().st_size, 100_000)
                            result = subprocess.run(
                                [
                                    "ffmpeg",
                                    "-nostdin",
                                    "-v",
                                    "error",
                                    "-xerror",
                                    "-i",
                                    str(output),
                                    "-map",
                                    "0:v:0",
                                    "-f",
                                    "null",
                                    "-",
                                ],
                                capture_output=True,
                                text=True,
                                timeout=120,
                            )
                            self.assertEqual(
                                result.returncode,
                                0,
                                f"Strict H.264 decoding failed: {result.stderr[:500]}",
                            )

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "FFmpeg and FFprobe required to validate muxing",
    )
    async def test_mux_audio_video_with_closed_captions(self):
        async with open_live_session(LiveSettings.from_env(), interval=0.75) as session:
            async with await ItunesApi.create(storefront="br") as itunes:
                streams = await MusicVideo.from_id(
                    "1874387735", api=session.api
                ).get_streams(
                    max_height=360, itunes_api=itunes, wrapper_api=session.wrapper
                )
                drm = Drm.from_widevine(session.api)
                async with (
                    Streamer(streams.video, drm=drm) as video,
                    Streamer(streams.audio, drm=drm) as audio,
                ):
                    video_bytes, audio_bytes = await asyncio.gather(
                        video.read(), audio.read()
                    )
                data = Muxer().mux(video=video_bytes, audio=audio_bytes)
                self.assertEqual(data[4:8], b"ftyp")
                self.assertEqual(data[8:12], b"M4V ")
                with TemporaryDirectory() as directory:
                    output = Path(directory) / "complete.m4v"
                    output.write_bytes(data)
                    probe = subprocess.run(
                        [
                            "ffprobe",
                            "-v",
                            "error",
                            "-show_entries",
                            "stream=codec_type,codec_name,codec_tag_string",
                            "-of",
                            "json",
                            str(output),
                        ],
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=30,
                    )
                    streams = json.loads(probe.stdout)["streams"]
                    self.assertEqual(
                        [(s["codec_type"], s["codec_name"]) for s in streams],
                        [("video", "h264"), ("subtitle", "eia_608"), ("audio", "aac")],
                    )
                    self.assertEqual(streams[0]["codec_tag_string"], "avc1")
                    self.assertEqual(streams[2]["codec_tag_string"], "mp4a")
                    decode = subprocess.run(
                        [
                            "ffmpeg",
                            "-nostdin",
                            "-v",
                            "error",
                            "-xerror",
                            "-i",
                            str(output),
                            "-map",
                            "0:v:0",
                            "-map",
                            "0:a:0",
                            "-f",
                            "null",
                            "-",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    self.assertEqual(decode.returncode, 0, decode.stderr[:500])


if __name__ == "__main__":
    unittest.main()
