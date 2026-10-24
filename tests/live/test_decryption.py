"""Opt-in live decryption smoke tests; downloads real media and requests licenses.

Run explicitly:
    GAMDL_RUN_LIVE_DECRYPT=1 uv run python -m unittest tests.live.test_decryption -v

The wrapper tests skip when no authenticated local wrapper is available.
Media is written only to temporary files; neither content keys nor signed
CDN URLs are printed or retained. Tests may require an Apple Music subscription.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from gamdl.drm import ContentKey, Drm
from gamdl.media import (
    HlsType,
    Song,
    SongCodec,
    SongStreamResolver,
    StreamOrigin,
)
from gamdl.stream import Stream
from tests.live_auth import LiveSettings, open_live_session

SONG_ID = "1559885421"


@unittest.skipUnless(
    os.environ.get("GAMDL_RUN_LIVE_DECRYPT") == "1",
    "Opt-in only: set GAMDL_RUN_LIVE_DECRYPT=1 to download and decrypt live media",
)
@unittest.skipUnless(
    shutil.which("ffmpeg"), "Requires FFmpeg for decoded audio validation"
)
class StreamDecryptionLiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = open_live_session(LiveSettings.from_env(), interval=0.75)
        self.session = await self.context.__aenter__()
        self.addAsyncCleanup(self.context.__aexit__, None, None, None)
        self.song = Song.from_id(SONG_ID, api=self.session.api)
        self.drm = Drm.from_widevine(self.session.api)
        self.directory = TemporaryDirectory(prefix="gamdl-decryption-test-")
        self.addCleanup(self.directory.cleanup)

    async def asset(self, origin: StreamOrigin, codec: SongCodec = SongCodec.AAC):
        sources = await SongStreamResolver(self.song).get_hls_streams(origin=origin)
        self.assertTrue(sources, "No HLS renditions for requested source")
        candidates = [track for track in sources if track.codec == codec]
        if not candidates:
            self.skipTest(f"{codec.value} not available from {origin.value}")
        return max(candidates, key=lambda track: track.bitrate or 0)

    async def download_and_check(self, source, *, key=None, wrapper=False):
        uri = None
        if wrapper:
            if self.session.wrapper is None:
                self.skipTest("No authenticated wrapper instance available")
            uri = (await source.get_drm_info()).fairplay_streaming
            if not uri:
                self.skipTest("No FairPlay URI in selected HLS stream")
        name = self.id().rsplit(".", 1)[-1] + ".m4a"
        path = Path(self.directory.name) / name
        async with Stream(
            source,
            wrapper_api=self.session.wrapper if wrapper else None,
            fairplay_key=uri,
            decryption_key=key,
        ) as stream:
            await stream.save(path)
            progress = stream.progress
            self.assertEqual(progress.completed_parts, progress.total_parts)
            self.assertGreater(progress.emitted_bytes, 4096)
            self.assertEqual(progress.emitted_bytes, path.stat().st_size)

        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(path),
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
        self.assertEqual(
            result.returncode,
            0,
            f"FFmpeg could not decode {source.codec.value} from {source.origin.value}: "
            + result.stderr[:320],
        )
        if shutil.which("mediainfo"):
            info = subprocess.run(
                ["mediainfo", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(info.returncode, 0)
            conformance = [
                line.strip()
                for line in info.stdout.splitlines()
                if "CMAF does not permit" in line or "channelConfigurationIndex" in line
            ]
            if conformance:
                print(
                    "MediaInfo codec/CMAF conformance notice "
                    f"for {source.codec.value} ({source.origin.value}): "
                    + "; ".join(conformance)[:350]
                )
        return path

    async def license_key(self, source) -> ContentKey:
        key = await self.drm.get_keys(
            media_id=self.song.id,
            drm_info=await source.get_drm_info(),
            is_library=self.song.is_library,
        )
        self.assertIsInstance(key, ContentKey)
        return key

    async def test_decrypt_web_aac_content_key(self):
        source = await self.song.get_stream(codec=SongCodec.AAC_WEB)
        self.assertEqual(source.origin, StreamOrigin.WEBPLAYBACK)
        await self.download_and_check(source, key=await self.license_key(source))

    async def test_decrypt_enhanced_hls_content_key(self):
        source = await self.asset(StreamOrigin.ASSET_ENHANCED_HLS)
        self.assertEqual(source.hls_type, HlsType.ENHANCED)
        decrypted = await self.download_and_check(
            source, key=await self.license_key(source)
        )
        # Compare MediaInfo against Apple's exact original encrypted bytes:
        # an upstream CMAF/USAC warning must not be blamed on decryption.
        raw = Path(self.directory.name) / "encrypted-original.m4a"
        async with Stream(source) as stream:
            await stream.save(raw)
        if shutil.which("mediainfo"):

            def notices(path):
                result = subprocess.run(
                    ["mediainfo", str(path)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                )
                return [
                    line.strip()
                    for line in result.stdout.splitlines()
                    if "CMAF does not permit USAC" in line
                ]

            self.assertEqual(notices(raw), notices(decrypted))
        # Decryption must not rewrite the codec's DecoderSpecificInfo.
        init = await source.get_init_segment()
        if init and b"esds" in init:
            pos = init.index(b"esds")
            size = int.from_bytes(init[pos - 4 : pos], "big")
            self.assertGreater(size, 8)
            self.assertEqual(
                init[pos - 4 : pos - 4 + size],
                decrypted.read_bytes()[pos - 4 : pos - 4 + size],
            )

    async def test_decrypt_standard_asset_hls_content_key(self):
        source = await self.asset(StreamOrigin.ASSET_STANDARD_HLS)
        self.assertEqual(source.hls_type, HlsType.STANDARD)
        await self.download_and_check(source, key=await self.license_key(source))

    async def test_wrapper_enhanced_hls(self):
        if self.session.wrapper is None:
            self.skipTest("No authenticated wrapper instance available")
        source = await self.song.get_stream(
            codec=SongCodec.AAC,
            wrapper_api=self.session.wrapper,
        )
        self.assertEqual(source.origin, StreamOrigin.WRAPPER_HLS)
        self.assertEqual(source.hls_type, HlsType.ENHANCED)
        await self.download_and_check(source, wrapper=True)

    async def test_wrapper_standard_asset_hls(self):
        if self.session.wrapper is None:
            self.skipTest("No authenticated wrapper instance available")
        source = await self.asset(StreamOrigin.ASSET_STANDARD_HLS)
        await self.download_and_check(source, wrapper=True)

    async def test_aac_he_mediainfo_diagnostic(self):
        # Extra smoke case to distinguish an HE-AAC codec/profile conformance
        # warning from an actual CBCS audio-decoding error.
        source = await self.asset(
            StreamOrigin.ASSET_ENHANCED_HLS, codec=SongCodec.AAC_HE
        )
        await self.download_and_check(source, key=await self.license_key(source))
