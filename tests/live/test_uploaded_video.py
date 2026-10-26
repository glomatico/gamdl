"""Optional real progressive uploaded-video regression.

Supply GAMDL_RUN_LIVE_DECRYPT=1 and GAMDL_LIVE_UPLOADED_VIDEO_ID to run.
No fixed ID is bundled because these uploads can be account-specific.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from gamdl.download import Downloader
from gamdl.media import UploadedVideo
from tests.live_auth import LiveSettings, open_live_session


@unittest.skipUnless(
    os.getenv("GAMDL_RUN_LIVE_DECRYPT") == "1"
    and bool(os.getenv("GAMDL_LIVE_UPLOADED_VIDEO_ID")),
    "Requires explicit uploaded-video ID and live-test opt-in",
)
@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and FFprobe required for live media validation",
)
class UploadedVideoLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_progressive_uploaded_video_download_and_decode(self):
        settings = LiveSettings.from_env()
        video_id = os.environ["GAMDL_LIVE_UPLOADED_VIDEO_ID"]
        async with open_live_session(settings, interval=0.75) as session:
            video = UploadedVideo.from_id(video_id, api=session.api)
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "upload.m4v"
                result = await Downloader().download(video, output)
                self.assertEqual(result.status, "downloaded")
                self.assertGreater(output.stat().st_size, 10000)
                probe = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-select_streams",
                        "V:0",
                        "-show_entries",
                        "stream=codec_name",
                        "-of",
                        "csv=p=0",
                        str(output),
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )
                self.assertTrue(probe.stdout.strip())
                subprocess.run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-xerror",
                        "-i",
                        str(output),
                        "-map",
                        "0:V:0",
                        "-f",
                        "null",
                        "-",
                    ],
                    capture_output=True,
                    check=True,
                    timeout=180,
                )
