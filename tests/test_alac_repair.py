"""Synthetic ALAC fixtures: no network access or copyrighted media."""

from __future__ import annotations

import asyncio
import json
import math
import random
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from gamdl.download import Downloader, Tags
from gamdl.media import Song, SongCodec
from gamdl.muxer import Muxer
from tests.test_muxer import description_fixture, packet_hashes


def packets(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_packets",
            "-show_entries",
            "packet=pos,size,pts,duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return [
        {k: int(v) for k, v in p.items()} for p in json.loads(result.stdout)["packets"]
    ]


def decode(path):
    return subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-f",
            "s32le",
            "-",
        ],
        capture_output=True,
    )


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "Requires FFmpeg"
)
class AlacRepairTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)

    def fixture(self, depth, channels, noise=False, silence=False):
        path = self.directory / "healthy.fmp4"
        rng = random.Random(32)
        pcm = bytearray()
        # Two complete frames and one partial frame, with independent channels.
        for index in range(8192 + 19):
            for channel in range(channels):
                limit = 1 << (depth - 1)
                value = (
                    rng.randrange(-limit, limit)
                    if noise
                    else int(math.sin(index * (channel + 1) * 0.031) * limit * 0.7)
                )
                if silence:
                    value = 0
                pcm.extend(
                    struct.pack(
                        "<h" if depth == 16 else "<i",
                        value if depth == 16 else value << 8,
                    )
                )
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-f",
                "s16le" if depth == 16 else "s32le",
                "-ar",
                "44100" if depth == 16 else "96000",
                "-ac",
                str(channels),
                "-i",
                "pipe:0",
                "-c:a",
                "alac",
                "-movflags",
                "+frag_keyframe+empty_moov+default_base_moof",
                "-frag_duration",
                "10000",
                "-f",
                "mp4",
                str(path),
            ],
            input=pcm,
            capture_output=True,
            check=True,
        )
        return path

    def corrupt(self, source, indices, tag=0):
        data = bytearray(source.read_bytes())
        for index, p in enumerate(packets(source)):
            if index not in indices:
                continue
            packet = data[p["pos"] : p["pos"] + p["size"]]
            # FFmpeg's encoder emits END followed only by byte-align zeros.
            # Locate that independent encoder output, not Gamdl's scanner.
            last_one = len(packet) * 8 - 1
            while not packet[last_one // 8] & (1 << (7 - last_one % 8)):
                last_one -= 1
            end = last_one - 2
            self.assertEqual(
                [(packet[b // 8] >> (7 - b % 8)) & 1 for b in range(end, end + 3)],
                [1, 1, 1],
            )
            for n in range(3):
                bit = end + n
                byte = p["pos"] + bit // 8
                mask = 1 << (7 - bit % 8)
                data[byte] = (data[byte] & ~mask) | (((tag >> (2 - n)) & 1) * mask)
        target = self.directory / "malformed.fmp4"
        target.write_bytes(data)
        return target

    def test_repairs_escape_and_compressed_frames_without_reencoding(self):
        for depth, channels, noise, silence in (
            (16, 2, True, False),
            (16, 2, False, False),
            (16, 1, False, True),
            (24, 2, False, False),
            (24, 6, False, False),
        ):
            with self.subTest(
                depth=depth, channels=channels, noise=noise, silence=silence
            ):
                healthy = self.fixture(depth, channels, noise, silence)
                expected = packet_hashes(healthy)
                good_pcm = decode(healthy)
                self.assertEqual(good_pcm.returncode, 0, good_pcm.stderr)
                # Exercise key-rotation description deduplication too.
                healthy.write_bytes(description_fixture(healthy.read_bytes()))
                self.assertEqual(packet_hashes(healthy), expected)
                malformed = self.corrupt(healthy, {1, 2}, tag=4 if noise else 0)
                self.assertNotEqual(decode(malformed).returncode, 0)
                self.assertNotEqual(packet_hashes(malformed), expected)
                memory = self.directory / "memory.m4a"
                disk = self.directory / "disk.m4a"
                memory.write_bytes(Muxer().remux(malformed.read_bytes()))
                Muxer().remux_file(malformed, disk)
                self.assertEqual(memory.read_bytes(), disk.read_bytes())
                self.assertEqual(packet_hashes(disk), expected)
                self.assertEqual(
                    [(p["size"], p["pts"], p["duration"]) for p in packets(disk)],
                    [(p["size"], p["pts"], p["duration"]) for p in packets(healthy)],
                )
                Muxer().tag(disk, tags=Tags(title="Repaired synthetic ALAC"))
                self.assertEqual(packet_hashes(disk), expected)
                repaired_pcm = decode(disk)
                self.assertEqual(repaired_pcm.returncode, 0, repaired_pcm.stderr)
                self.assertEqual(repaired_pcm.stdout, good_pcm.stdout)
                disabled = self.directory / "disabled.m4a"
                Muxer(repair_alac=False).remux_file(malformed, disabled)
                self.assertEqual(packet_hashes(disabled), packet_hashes(malformed))
                self.assertNotEqual(decode(disabled).returncode, 0)

    def test_healthy_packets_are_identical_with_repair_enabled_or_disabled(self):
        for depth, channels, noise in ((16, 2, True), (16, 1, False), (24, 2, False)):
            with self.subTest(depth=depth, channels=channels, noise=noise):
                healthy = self.fixture(depth, channels, noise)
                data = healthy.read_bytes()
                expected = Muxer(repair_alac=False).remux(data)
                self.assertEqual(Muxer().remux(data), expected)
                output = self.directory / "healthy.m4a"
                Muxer().remux_file(healthy, output)
                self.assertEqual(output.read_bytes(), expected)
                self.assertEqual(packet_hashes(output), packet_hashes(healthy))

    def test_truncated_audio_is_not_repaired(self):
        healthy = self.fixture(16, 2, True)
        malformed = self.corrupt(healthy, {1}, tag=0)
        data = bytearray(malformed.read_bytes())
        p = packets(malformed)[1]
        # Declare more PCM than this packet contains; do not touch MP4 tables.
        data[p["pos"] + 2] |= 0x10
        data[p["pos"] + 3 : p["pos"] + 7] = b"\xff" * 4
        self.assertEqual(
            Muxer().remux(bytes(data)), Muxer(repair_alac=False).remux(bytes(data))
        )

    def test_default_downloader_repairs_before_tagging_for_both_apis(self):
        healthy = self.fixture(16, 2, True)
        expected = packet_hashes(healthy)
        malformed = self.corrupt(healthy, {1}, tag=4)
        original = malformed.read_bytes()

        async def run():
            song = Song.from_id("111", api=Mock())
            song.get_stream = AsyncMock(
                return_value=Mock(codec=SongCodec.ALAC_LOSSLESS)
            )
            song.get_cover = AsyncMock(return_value=None)
            song.get_lyrics = AsyncMock(return_value=None)
            resolver = Mock()
            resolver.get_tags = AsyncMock(
                return_value=Tags(title="ALAC repair fixture")
            )
            downloader = Downloader(tag_resolver=resolver)
            downloader._read_track = AsyncMock(return_value=(original, "none"))

            async def save(_id, _source, _track, destination, _callback, **kwargs):
                destination.write_bytes(original)
                return "none"

            downloader._save_track = AsyncMock(side_effect=save)
            memory = self.directory / "download-bytes.m4a"
            memory.write_bytes(
                await downloader.get_bytes(song, codec=SongCodec.ALAC_LOSSLESS)
            )
            disk = self.directory / "download-file.m4a"
            await downloader.download(song, disk, codec=SongCodec.ALAC_LOSSLESS)
            for path in (memory, disk):
                self.assertEqual(packet_hashes(path), expected)
                result = decode(path)
                self.assertEqual(result.returncode, 0, result.stderr)

        asyncio.run(run())
        self.assertEqual(malformed.read_bytes(), original)

    def test_non_alac_packets_bypass_repair(self):
        for codec in ("aac", "ac3", "eac3"):
            with self.subTest(codec=codec):
                source = self.directory / f"{codec}.fmp4"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        "sine=sample_rate=48000:duration=0.3",
                        "-c:a",
                        codec,
                        "-movflags",
                        "+frag_keyframe+delay_moov+default_base_moof",
                        "-frag_duration",
                        "100000",
                        "-f",
                        "mp4",
                        str(source),
                    ],
                    capture_output=True,
                    check=True,
                )
                data = source.read_bytes()
                expected = Muxer(repair_alac=False).remux(data)
                self.assertEqual(Muxer().remux(data), expected)
                output = self.directory / f"{codec}.m4a"
                Muxer().remux_file(source, output)
                self.assertEqual(output.read_bytes(), expected)
                self.assertEqual(packet_hashes(output), packet_hashes(source))
                result = decode(output)
                self.assertEqual(result.returncode, 0, result.stderr)
