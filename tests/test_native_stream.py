"""Local HTTP fixtures exercise native Tokio/reqwest, not HTTPX mocking."""

import asyncio
import hashlib
import shutil
import subprocess
import threading
import time
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock

import m3u8

from gamdl.drm import ContentKey, Drm, DRMInfo
from gamdl.media import (
    AudioStream,
    HttpStream,
    SongCodec,
    StreamManifestError,
    StreamOrigin,
    VideoStream,
)
from gamdl.stream import Streamer, StreamProgress


class HttpHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.split("?", 1)[0].lstrip("/")
        with self.server.lock:
            self.server.hits[name] += 1
            count = self.server.hits[name]
        if name == "retry" and count == 1:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if name == "missing":
            self.send_response(404)
            self.end_headers()
            return
        if name == "interrupt" and count == 1:
            self.send_response(200)
            self.send_header("Content-Length", "30")
            self.end_headers()
            self.wfile.write(b"HALF")
            self.close_connection = True
            return
        if name == "progressive-drop":
            size = 8192
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            self.wfile.write(b"Q" * (3072 if count == 1 else size))
            if count == 1:
                self.close_connection = True
            return
        if name == "large-progressive":
            size = 65 * 1024 * 1024 + 42
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            block = b"Z" * (1024 * 1024)
            try:
                for _ in range(65):
                    self.wfile.write(block)
                self.wfile.write(b"Z" * 42)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if name == "progressive-unknown-length":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"NO CONTENT LENGTH")
            self.close_connection = True
            return
        if name == "slow":
            time.sleep(0.35)
        bodies = {
            "init": b"INIT",
            "a": b"A" * 3000,
            "b": b"B" * 4000,
            "c": b"C" * 3000,
            "retry": b"RETRIED",
            "interrupt": b"COMPLETE",
            "range": b"0123456789",
            "ignore-range": b"0123456789",
            "slow": b"S" * 5000,
            **self.server.fixtures,
        }
        body = bodies.get(name)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        requested = self.headers.get("Range")
        status = 200
        if requested and name != "ignore-range":
            try:
                assert requested.startswith("bytes=")
                start, end = map(int, requested[6:].split("-"))
            except (AssertionError, ValueError):
                self.send_response(416)
                self.end_headers()
                return
            status = 206
            selected = body[start : end + 1]
        else:
            selected = body
        self.send_response(status)
        self.send_header("Content-Length", str(len(selected)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
        self.end_headers()
        try:
            self.wfile.write(selected)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        pass


def manifest_url(url: str, content: str):
    return m3u8.loads(content, uri=f"{url}/media.m3u8")


def fixture_playlist(segments: str, *, prefix: str = ""):
    return "#EXTM3U\n#EXT-X-VERSION:7\n" + prefix + segments + "#EXT-X-ENDLIST\n"


class NativeStreamTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), HttpHandler)
        cls.http.lock = threading.Lock()
        cls.http.hits = Counter()
        cls.http.fixtures = {}
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.http.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()

    def setUp(self):
        self.http.hits.clear()
        self.http.fixtures.clear()

    def audio(self, content: str):
        transport = Mock()
        transport.get_manifest = AsyncMock(return_value=manifest_url(self.url, content))
        return AudioStream(
            codec=SongCodec.AAC_WEB,
            origin=StreamOrigin.WEBPLAYBACK,
            url=f"{self.url}/media.m3u8",
            transport=transport,
        )

    async def test_progressive_http_download_and_progress(self):
        source = HttpStream(self.url + "/a", codec=SongCodec.AAC_WEB)
        self.assertFalse((await source.get_drm_info()).is_protected)
        streamer = Streamer(source, chunk_size=1024)
        with self.assertRaisesRegex(RuntimeError, "after entering"):
            _ = streamer.decryption_method
        async with streamer:
            self.assertEqual(streamer.decryption_method, "none")
            self.assertEqual(streamer.progress.total_parts, 1)
            # The HTTP response headers arrive asynchronously after enter.
            chunks = [chunk async for chunk in streamer]
            self.assertEqual(b"".join(chunks), b"A" * 3000)
            self.assertTrue(all(0 < len(chunk) <= 1024 for chunk in chunks))
            self.assertEqual(streamer.progress.total_bytes, 3000)
            self.assertEqual(streamer.progress.completed_parts, 1)
        self.assertEqual(streamer.progress.emitted_bytes, 3000)
        self.assertEqual(streamer.progress.percentage, 100.0)
        self.assertEqual(self.http.hits["a"], 1)

    async def test_progressive_http_ignores_unavailable_hls_drm(self):
        source = HttpStream(self.url + "/b")
        drm = Drm.from_widevine(Mock())
        drm.get_keys = AsyncMock(side_effect=AssertionError("No HLS DRM data"))
        async with Streamer(source, drm=drm) as streamer:
            self.assertEqual(streamer.decryption_method, "none")
            self.assertEqual(await streamer.read(), b"B" * 4000)
        drm.get_keys.assert_not_awaited()

    async def test_drm_key_lifecycle_callback_is_tied_to_real_license_request(self):
        source = self.audio(fixture_playlist("#EXTINF:1,\na\n"))
        source.media_id = "111"
        source.get_drm_info = AsyncMock(
            return_value=DRMInfo(
                widevine_pssh=b"pssh",
                widevine_uri="https://license.example.test/widevine",
            )
        )
        drm = Drm.from_widevine(Mock())
        drm.get_keys = AsyncMock(return_value=ContentKey(b"K" * 16, b"S" * 16))
        events = []
        async with Streamer(
            source, drm=drm, key_event_callback=events.append
        ) as stream:
            self.assertEqual(stream.decryption_method, "widevine")
        self.assertEqual(events, ["started", "completed"])
        drm.get_keys.assert_awaited_once()

        drm.get_keys = AsyncMock(side_effect=RuntimeError("license failed"))
        events.clear()
        with self.assertRaisesRegex(RuntimeError, "license failed"):
            async with Streamer(source, drm=drm, key_event_callback=events.append):
                pass
        self.assertEqual(events, ["started", "failed"])

    async def test_downloader_event_reports_real_native_transfer_progress(self):
        from gamdl.download import Downloader, DownloadStage, EventState

        events = []
        callback_progress = []
        payload, method = await Downloader()._read_track(
            "id",
            HttpStream(self.url + "/a"),
            "audio",
            callback_progress.append,
            event_callback=events.append,
        )
        self.assertEqual(payload, b"A" * 3000)
        self.assertEqual(method, "none")
        self.assertEqual(events[0].stage, DownloadStage.DOWNLOADING)
        self.assertEqual(events[0].state, EventState.STARTED)
        self.assertEqual(events[-1].state, EventState.COMPLETED)
        self.assertEqual(events[-1].stage, DownloadStage.DOWNLOADING)
        transfer = [e for e in events if e.state == EventState.PROGRESS]
        self.assertTrue(transfer)
        self.assertEqual(transfer[-1].progress.emitted_bytes, 3000)
        self.assertFalse(
            any(e.stage == DownloadStage.FETCHING_DECRYPTION_KEYS for e in events)
        )

    async def test_separate_audio_video_streamers_run_concurrently(self):
        playlist = fixture_playlist("#EXTINF:1,\na\n")
        audio = self.audio(playlist)
        video = VideoStream(
            codec="h264",
            origin=StreamOrigin.ITUNES_HLS,
            url=self.url + "/video.m3u8",
            transport=audio._transport,
        )
        async with Streamer(audio) as audio_dl, Streamer(video) as video_dl:
            a, v = await asyncio.gather(audio_dl.read(), video_dl.read())
        self.assertEqual(a, b"A" * 3000)
        self.assertEqual(v, b"A" * 3000)
        self.assertEqual(audio_dl.decryption_method, "none")
        self.assertEqual(video_dl.decryption_method, "none")

    async def test_progressive_http_large_file_is_streamed_without_part_limit(self):
        source = HttpStream(self.url + "/large-progressive")
        size = 65 * 1024 * 1024 + 42
        digest = hashlib.sha256()
        count = 0
        async with Streamer(source, chunk_size=512 * 1024) as streamer:
            async for block in streamer:
                self.assertLessEqual(len(block), 512 * 1024)
                digest.update(block)
                count += len(block)
            self.assertEqual(streamer.progress.total_bytes, size)
        self.assertEqual(count, size)
        self.assertEqual(digest.digest(), hashlib.sha256(b"Z" * size).digest())

    async def test_progressive_http_retries_before_emission(self):
        source = HttpStream(self.url + "/retry")
        async with Streamer(source, retries=2, chunk_size=1024) as streamer:
            self.assertEqual(await streamer.read(), b"RETRIED")
        self.assertEqual(self.http.hits["retry"], 2)

    async def test_progressive_http_without_content_length(self):
        async with Streamer(
            HttpStream(self.url + "/progressive-unknown-length")
        ) as streamer:
            self.assertEqual(await streamer.read(), b"NO CONTENT LENGTH")
            self.assertIsNone(streamer.progress.total_bytes)
            self.assertEqual(streamer.progress.percentage, 100.0)

    async def test_progressive_http_interruption_cannot_publish_partial_file(self):
        source = HttpStream(self.url + "/progressive-drop")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "output.mp4"
            path.write_bytes(b"ORIGINAL")
            async with Streamer(source, chunk_size=1024, retries=2) as streamer:
                with self.assertRaisesRegex(OSError, "incomplete"):
                    await streamer.save(path)
            self.assertEqual(path.read_bytes(), b"ORIGINAL")
            self.assertEqual(
                [p.name for p in Path(directory).iterdir()], ["output.mp4"]
            )
            self.assertEqual(self.http.hits["progressive-drop"], 1)

    async def test_progressive_http_invalid_url_and_decryption_key(self):
        with self.assertRaisesRegex(ValueError, "decryption"):
            Streamer(HttpStream(self.url + "/a"), decryption_key=b"0" * 16)
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            async with Streamer(HttpStream("http://example.test/secret.mp4")):
                pass
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            async with Streamer(HttpStream("https://name:pw@example.test/a")):
                pass
        with self.assertRaisesRegex(TypeError, "HlsStream or HttpStream"):
            Streamer("https://example.test/audio.mp3")

    async def test_ordered_chunks_and_init_once(self):
        playlist = fixture_playlist(
            "#EXTINF:4,\na\n#EXTINF:4,\nb\n#EXTINF:4,\nc\n",
            prefix='#EXT-X-MAP:URI="init"\n',
        )
        audio = self.audio(playlist)
        async with Streamer(audio, concurrency=3, chunk_size=1024) as stream:
            chunks = [chunk async for chunk in stream]
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(0 < len(chunk) <= 1024 for chunk in chunks))
        self.assertEqual(
            b"".join(chunks), b"INIT" + b"A" * 3000 + b"B" * 4000 + b"C" * 3000
        )
        self.assertEqual(self.http.hits["init"], 1)
        audio._transport.get_manifest.assert_awaited_once()

    async def test_ordered_fetches_progress_when_byte_budget_limits_admission(self):
        from gamdl import _native

        # Unknown-size requests reserve 64 MiB before admission. More than
        # four at concurrency=8 must still make progress with a 256-MiB budget.
        parts = [
            (f"{self.url}/{name}", None, None)
            for name in ["slow"] + ["a", "b", "c"] * 5
        ]
        stream = _native.NativeStream(parts, 8)

        async def consume():
            chunks = []
            while (chunk := await stream.next_chunk()) is not None:
                chunks.append(chunk)
            return b"".join(chunks)

        try:
            data = await asyncio.wait_for(consume(), 10)
        finally:
            stream.close()
        self.assertEqual(
            data, b"S" * 5000 + (b"A" * 3000 + b"B" * 4000 + b"C" * 3000) * 5
        )

    async def test_cancellation_releases_global_payload_reservations(self):
        from gamdl import _native

        streams = [
            _native.NativeStream([(f"{self.url}/slow", None, None)] * 8, 8)
            for _ in range(6)
        ]
        await asyncio.sleep(0.05)
        for stream in streams:
            stream.close()
        streams.clear()
        del stream
        # A fresh stream shares the same process-wide budget. Leaked permits
        # from cancelled workers (including admission waiters) would block it.
        replacement = _native.NativeStream([(f"{self.url}/a", None, None)], 1)
        try:
            self.assertEqual(
                await asyncio.wait_for(replacement.next_chunk(), 10), b"A" * 3000
            )
            self.assertIsNone(await asyncio.wait_for(replacement.next_chunk(), 10))
        finally:
            replacement.close()

    async def test_byte_ranges_and_implicit_offsets(self):
        playlist = fixture_playlist(
            "#EXT-X-BYTERANGE:4@1\n#EXTINF:1,\nrange\n"
            "#EXT-X-BYTERANGE:3\n#EXTINF:1,\nrange\n",
            prefix='#EXT-X-MAP:URI="range",BYTERANGE="2@0"\n',
        )
        async with Streamer(self.audio(playlist)) as stream:
            self.assertEqual(await stream.read(), b"01" + b"1234" + b"567")
        self.assertEqual(self.http.hits["range"], 3)

    async def test_retry_recovers_503_without_duplicating_data(self):
        playlist = fixture_playlist("#EXTINF:1,\nretry\n")
        async with Streamer(self.audio(playlist), retries=2) as stream:
            self.assertEqual(await stream.read(), b"RETRIED")
        self.assertEqual(self.http.hits["retry"], 2)

    async def test_interrupted_response_restarts_complete_fragment(self):
        playlist = fixture_playlist("#EXTINF:1,\ninterrupt\n")
        async with Streamer(self.audio(playlist), retries=2) as stream:
            self.assertEqual(await stream.read(), b"COMPLETE")
        self.assertEqual(self.http.hits["interrupt"], 2)

    async def test_new_init_section_inserted_on_map_change(self):
        playlist = fixture_playlist(
            '#EXT-X-MAP:URI="init"\n'
            "#EXTINF:1,\na\n"
            '#EXT-X-MAP:URI="range"\n'
            "#EXTINF:1,\nb\n",
        )
        async with Streamer(self.audio(playlist)) as stream:
            self.assertEqual(
                await stream.read(),
                b"INIT" + b"A" * 3000 + b"0123456789" + b"B" * 4000,
            )
        self.assertEqual(self.http.hits["init"], 1)
        self.assertEqual(self.http.hits["range"], 1)

    async def test_range_ignored_fails_without_publishing_partial_output(self):
        playlist = fixture_playlist("#EXT-X-BYTERANGE:4@0\n#EXTINF:1,\nignore-range\n")
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.m4a"
            path.write_bytes(b"PREEXISTING")
            async with Streamer(self.audio(playlist)) as stream:
                with self.assertRaisesRegex(OSError, "unexpected HTTP status"):
                    await stream.save(path)
            self.assertEqual(path.read_bytes(), b"PREEXISTING")
            self.assertEqual(
                sorted(p.name for p in Path(tmp).iterdir()), ["output.m4a"]
            )

    async def test_failed_fragment_does_not_publish(self):
        playlist = fixture_playlist("#EXTINF:1,\na\n#EXTINF:1,\nmissing\n")
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.m4a"
            async with Streamer(self.audio(playlist), concurrency=2) as stream:
                with self.assertRaises(OSError):
                    await stream.save(path)
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    async def test_exit_cancels_background_tasks(self):
        playlist = fixture_playlist("#EXTINF:1,\nslow\n#EXTINF:1,\nslow\n")
        async with Streamer(self.audio(playlist)) as stream:
            task = asyncio.create_task(stream.__anext__())
            await asyncio.sleep(0.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with self.assertRaisesRegex(RuntimeError, "async with"):
            await stream.__anext__()

    async def test_context_required_and_single_use(self):
        source = self.audio(fixture_playlist("#EXTINF:1,\na\n"))
        instance = Streamer(source)
        with self.assertRaises(RuntimeError):
            await instance.read()
        async with instance as stream:
            self.assertEqual(await stream.read(), b"A" * 3000)
        with self.assertRaisesRegex(RuntimeError, "cannot be reopened"):
            async with instance:
                pass

    async def test_invalid_native_config_or_url(self):
        source = self.audio(fixture_playlist("#EXTINF:1,\na\n"))
        with self.assertRaises(ValueError):
            async with Streamer(source, concurrency=0):
                pass
        playlist = fixture_playlist("#EXTINF:1,\nhttp://evil.test/file\n")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            async with Streamer(self.audio(playlist)):
                pass

    async def test_reject_unbounded_and_unsupported_playlists(self):
        examples = [
            "#EXTM3U\n#EXTINF:1,\na\n",
            "#EXTM3U\n#EXT-X-ENDLIST\n",
            fixture_playlist(
                "#EXTINF:1,\na\n",
                prefix='#EXT-X-KEY:METHOD=AES-128,URI="https://example.test/key"\n',
            ),
            fixture_playlist("#EXT-X-DISCONTINUITY\n#EXTINF:1,\na\n"),
        ]
        for text in examples:
            with self.subTest(text=text):
                with self.assertRaises(StreamManifestError):
                    async with Streamer(self.audio(text)):
                        pass

    async def test_save_and_read(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.m4a"
            async with Streamer(
                self.audio(fixture_playlist("#EXTINF:1,\na\n"))
            ) as stream:
                returned = await stream.save(path)
            self.assertEqual(returned, path)
            self.assertEqual(path.read_bytes(), b"A" * 3000)

    async def test_native_progress_snapshots_and_callback(self):
        playlist = fixture_playlist(
            "#EXTINF:1,\na\n#EXTINF:1,\nb\n",
        )
        observations = []
        source = self.audio(playlist)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.m4a"
            stream = Streamer(source, chunk_size=1024)
            with self.assertRaises(RuntimeError):
                _ = stream.progress
            async with stream:
                self.assertEqual(stream.progress.total_parts, 2)
                await stream.save(path, progress_callback=observations.append)
                self.assertEqual(stream.progress.completed_parts, 2)
            final = stream.progress
            self.assertIsInstance(final, StreamProgress)
            self.assertEqual(final.downloaded_bytes, 7000)
            self.assertEqual(final.emitted_bytes, 7000)
            self.assertEqual(final.total_parts, 2)
            self.assertIsNone(final.total_bytes)
            self.assertEqual(final.percentage, 100.0)
            self.assertGreater(final.speed, 0)
            self.assertTrue(observations)
            self.assertEqual(observations[-1].emitted_bytes, 7000)
            self.assertEqual(path.stat().st_size, 7000)

    async def test_known_progress_byte_ranges(self):
        source = self.audio(
            fixture_playlist(
                "#EXT-X-BYTERANGE:4@0\n#EXTINF:1,\nrange\n"
                "#EXT-X-BYTERANGE:3\n#EXTINF:1,\nrange\n",
            )
        )
        async with Streamer(source) as stream:
            self.assertEqual(stream.progress.total_bytes, 7)
            self.assertEqual(await stream.read(), b"0123456")
        self.assertEqual(stream.progress.percentage, 100.0)

    @unittest.skipUnless(
        shutil.which("ffmpeg"), "FFmpeg required for CENC video fixture"
    )
    async def test_decrypted_cenc_video_matches_ffmpeg(self):
        key = bytes.fromhex("00112233445566778899aabbccddeeff")
        kid = bytes.fromhex("101112131415161718191a1b1c1d1e1f")
        with TemporaryDirectory() as tmp:
            encrypted = Path(tmp) / "encrypted-video.mp4"
            output = Path(tmp) / "decrypted-video.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=128x72:rate=15:duration=0.8",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+frag_keyframe+empty_moov",
                    "-encryption_scheme",
                    "cenc-aes-ctr",
                    "-encryption_key",
                    key.hex(),
                    "-encryption_kid",
                    kid.hex(),
                    "-y",
                    str(encrypted),
                ],
                check=True,
                capture_output=True,
            )
            self.http.fixtures["video-encrypted"] = encrypted.read_bytes()
            stream_source = VideoStream(
                codec="h264",
                width=128,
                height=72,
                origin=StreamOrigin.WRAPPER_HLS,
                url=f"{self.url}/video.m3u8",
                transport=self.audio(
                    fixture_playlist("#EXTINF:1,\nvideo-encrypted\n")
                )._transport,
            )
            async with Streamer(
                stream_source, decryption_key=ContentKey(kid, key)
            ) as stream:
                await stream.save(output)

            def checksum(cmd):
                return subprocess.run(
                    cmd + ["-an", "-f", "md5", "-"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()

            reference = checksum(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-v",
                    "error",
                    "-decryption_key",
                    key.hex(),
                    "-i",
                    str(encrypted),
                ]
            )
            actual = checksum(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-v",
                    "error",
                    "-i",
                    str(output),
                ]
            )
            self.assertEqual(actual, reference)
            self.assertIn(b"avc1", output.read_bytes())
            self.assertNotIn(b"encv", output.read_bytes())

    @unittest.skipUnless(
        shutil.which("ffmpeg"), "FFmpeg required for encrypted MP4 fixtures"
    )
    async def test_decrypted_stream_is_decodable_aac(self):
        key = bytes.fromhex("00112233445566778899aabbccddeeff")
        kid = bytes.fromhex("101112131415161718191a1b1c1d1e1f")
        with TemporaryDirectory() as tmp:
            clear = Path(tmp) / "clear.m4a"
            encrypted = Path(tmp) / "encrypted.m4a"
            output = Path(tmp) / "decrypted.m4a"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=800:duration=0.8",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-y",
                    str(clear),
                ],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-v",
                    "error",
                    "-i",
                    str(clear),
                    "-c:a",
                    "copy",
                    "-movflags",
                    "+frag_keyframe+empty_moov",
                    "-encryption_scheme",
                    "cenc-aes-ctr",
                    "-encryption_key",
                    key.hex(),
                    "-encryption_kid",
                    kid.hex(),
                    "-y",
                    str(encrypted),
                ],
                check=True,
                capture_output=True,
            )
            self.http.fixtures["encrypted"] = encrypted.read_bytes()
            playlist = fixture_playlist("#EXTINF:1,\nencrypted\n")
            async with Streamer(
                self.audio(playlist),
                decryption_key=ContentKey(kid, key),
            ) as stream:
                await stream.save(output)

            def md5(path: Path):
                return subprocess.run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-v",
                        "error",
                        "-i",
                        str(path),
                        "-map",
                        "0:a:0",
                        "-f",
                        "md5",
                        "-",
                    ],
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout.strip()

            ffmpeg_decrypted = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-v",
                    "error",
                    "-decryption_key",
                    key.hex(),
                    "-i",
                    str(encrypted),
                    "-map",
                    "0:a:0",
                    "-f",
                    "md5",
                    "-",
                ],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            self.assertEqual(ffmpeg_decrypted, md5(output))
            self.assertNotEqual(encrypted.read_bytes(), output.read_bytes())
            self.assertNotIn(b"enca", output.read_bytes())
            self.assertNotIn(b"sinf", output.read_bytes())
            self.assertIn(b"mp4a", output.read_bytes())
            # Reuse the same encrypted fixture but separate EXT-X-MAP from
            # the media fragments, exercising native cross-part state.
            original = encrypted.read_bytes()
            split = original.index(b"moof") - 4
            self.http.fixtures["init-encrypted"] = original[:split]
            self.http.fixtures["fragment-encrypted"] = original[split:]
            playlist = fixture_playlist(
                "#EXTINF:1,\nfragment-encrypted\n",
                prefix='#EXT-X-MAP:URI="init-encrypted"\n',
            )
            async with Streamer(
                self.audio(playlist),
                decryption_key=key,
            ) as stream:
                chunks = [chunk async for chunk in stream]
            separated = Path(tmp) / "split-decrypted.m4a"
            separated.write_bytes(b"".join(chunks))
            self.assertEqual(md5(separated), ffmpeg_decrypted)
            self.assertEqual(separated.read_bytes(), output.read_bytes())
            self.http.fixtures.clear()

    @unittest.skipUnless(
        shutil.which("ffmpeg"), "FFmpeg required for encrypted MP4 fixtures"
    )
    async def test_wrong_key_id_rejected(self):
        key = bytes.fromhex("00112233445566778899aabbccddeeff")
        kid = bytes.fromhex("101112131415161718191a1b1c1d1e1f")
        with TemporaryDirectory() as tmp:
            encrypted = Path(tmp) / "encrypted.m4a"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=800:duration=0.2",
                    "-c:a",
                    "aac",
                    "-movflags",
                    "+frag_keyframe+empty_moov",
                    "-encryption_scheme",
                    "cenc-aes-ctr",
                    "-encryption_key",
                    key.hex(),
                    "-encryption_kid",
                    kid.hex(),
                    "-y",
                    str(encrypted),
                ],
                check=True,
                capture_output=True,
            )
            self.http.fixtures["encrypted"] = encrypted.read_bytes()
            playlist = fixture_playlist("#EXTINF:1,\nencrypted\n")
            with self.assertRaisesRegex(ValueError, "16 bytes"):
                async with Streamer(self.audio(playlist), decryption_key=b"too short"):
                    pass
            async with Streamer(
                self.audio(playlist),
                decryption_key=ContentKey(b"0" * 16, key),
            ) as stream:
                with self.assertRaisesRegex(OSError, "key ID"):
                    await stream.read()
            self.http.fixtures.clear()

    @unittest.skipUnless(
        shutil.which("ffmpeg"), "FFmpeg required for encrypted MP4 fixtures"
    )
    async def test_unsupported_cbcs_and_missing_init_fail_closed(self):
        key = bytes.fromhex("00112233445566778899aabbccddeeff")
        kid = bytes.fromhex("101112131415161718191a1b1c1d1e1f")
        with TemporaryDirectory() as tmp:
            encrypted = Path(tmp) / "encrypted.m4a"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=800:duration=0.2",
                    "-c:a",
                    "aac",
                    "-movflags",
                    "+frag_keyframe+empty_moov",
                    "-encryption_scheme",
                    "cenc-aes-ctr",
                    "-encryption_key",
                    key.hex(),
                    "-encryption_kid",
                    kid.hex(),
                    "-y",
                    str(encrypted),
                ],
                check=True,
                capture_output=True,
            )
            original = encrypted.read_bytes()
            self.http.fixtures["encrypted"] = original.replace(b"cenc", b"cbcs", 1)
            playlist = fixture_playlist("#EXTINF:1,\nencrypted\n")
            async with Streamer(self.audio(playlist), decryption_key=key) as stream:
                with self.assertRaisesRegex(OSError, "Unsupported fragmented MP4"):
                    await stream.read()
            split = original.index(b"moof") - 4
            self.http.fixtures["encrypted"] = original[split:]
            async with Streamer(self.audio(playlist), decryption_key=key) as stream:
                with self.assertRaisesRegex(OSError, "initialization section"):
                    await stream.read()
