"""Opt-in end-to-end Downloader tests using real Apple Music media.

Run explicitly with an authenticated test environment:
    GAMDL_RUN_LIVE_DECRYPT=1 uv run python -m unittest tests.live.test_downloader -v

The default offline test suite never downloads licensed media.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from gamdl.api import ItunesApi
from gamdl.download import Downloader
from gamdl.drm import Drm
from gamdl.media import MusicVideo, Song, SongCodec

from tests.live_auth import LiveSettings, open_live_session


def _probe(path: Path, kind: str) -> str:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            kind,
            "-show_entries",
            "stream=codec_name,codec_tag_string",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def _strict_decode(path: Path, *, video: bool) -> None:
    args = ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path)]
    if video:
        args.extend(["-map", "0:V:0"])  # Exclude cover-art attached pictures.
    args.extend(["-map", "0:a:0", "-f", "null", "-"])
    subprocess.run(args, capture_output=True, check=True, timeout=180)


@unittest.skipUnless(
    os.environ.get("GAMDL_RUN_LIVE_DECRYPT") == "1",
    "Full downloader tests require explicit live authentication opt-in",
)
@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and FFprobe required for full downloader validation",
)
class DownloaderLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_tagged_song_and_strict_decode(self):
        settings = LiveSettings.from_env()
        async with open_live_session(settings, interval=0.75) as session:
            downloader = Downloader(
                drm=Drm.from_widevine(session.api), wrapper_api=session.wrapper
            )
            song = Song.from_id("1228739609", api=session.api)
            with TemporaryDirectory() as directory:
                output = Path(directory) / "song.m4a"
                result = await downloader.download(
                    song, output, codec=SongCodec.AAC_WEB
                )
                self.assertEqual(result.status, "downloaded")
                self.assertGreater(output.stat().st_size, 100_000)
                with output.open("rb") as file:
                    self.assertEqual(file.read(12)[8:12], b"M4A ")
                self.assertIn("aac,mp4a", _probe(output, "a:0"))
                _strict_decode(output, video=False)

    async def test_tagged_video_and_moving_video_strict_decode(self):
        settings = LiveSettings.from_env()
        async with open_live_session(settings, interval=0.75) as session:
            async with await ItunesApi.create(storefront=settings.storefront) as itunes:
                downloader = Downloader(
                    drm=Drm.from_widevine(session.api),
                    wrapper_api=session.wrapper,
                    itunes_api=itunes,
                )
                video = MusicVideo.from_id("299199411", api=session.api)
                with TemporaryDirectory() as directory:
                    output = Path(directory) / "video.mp4"
                    result = await downloader.download(video, output, max_height=360)
                    self.assertEqual(result.status, "downloaded")
                    self.assertGreater(output.stat().st_size, 100_000)
                    with output.open("rb") as file:
                        self.assertEqual(file.read(12)[8:12], b"mp42")
                    self.assertIn("h264,avc1", _probe(output, "V:0"))
                    self.assertIn("aac,mp4a", _probe(output, "a:0"))
                    _strict_decode(output, video=True)
