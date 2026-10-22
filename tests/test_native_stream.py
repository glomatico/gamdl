"""Local HTTP fixtures exercise native Tokio/reqwest, not HTTPX mocking."""

import asyncio
import shutil
import subprocess
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock

import m3u8

from gamdl.media import AudioStream, SongCodec, StreamManifestError, StreamOrigin
from gamdl.drm import ContentKey
from gamdl.stream import Stream


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
        if name == "slow":
            time.sleep(0.35)
        if name in self.server.fixtures:
            bodies = self.server.fixtures
        else:
            bodies = {}
        bodies = {
            **{
                "init": b"INIT",
                "a": b"A" * 3000,
                "b": b"B" * 4000,
                "c": b"C" * 3000,
                "retry": b"RETRIED",
                "interrupt": b"COMPLETE",
                "range": b"0123456789",
                "ignore-range": b"0123456789",
                "slow": b"S" * 5000,
            },
            **bodies,
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

    async def test_ordered_chunks_and_init_once(self):
        playlist = fixture_playlist(
            "#EXTINF:4,\na\n#EXTINF:4,\nb\n#EXTINF:4,\nc\n",
            prefix='#EXT-X-MAP:URI="init"\n',
        )
        audio = self.audio(playlist)
        async with Stream(audio, concurrency=3, chunk_size=1024) as stream:
            chunks = [chunk async for chunk in stream]
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(0 < len(chunk) <= 1024 for chunk in chunks))
        self.assertEqual(
            b"".join(chunks), b"INIT" + b"A" * 3000 + b"B" * 4000 + b"C" * 3000
        )
        self.assertEqual(self.http.hits["init"], 1)
        audio._transport.get_manifest.assert_awaited_once()

    async def test_byte_ranges_and_implicit_offsets(self):
        playlist = fixture_playlist(
            "#EXT-X-BYTERANGE:4@1\n#EXTINF:1,\nrange\n"
            "#EXT-X-BYTERANGE:3\n#EXTINF:1,\nrange\n",
            prefix='#EXT-X-MAP:URI="range",BYTERANGE="2@0"\n',
        )
        async with Stream(self.audio(playlist)) as stream:
            self.assertEqual(await stream.read(), b"01" + b"1234" + b"567")
        self.assertEqual(self.http.hits["range"], 3)

    async def test_retry_recovers_503_without_duplicating_data(self):
        playlist = fixture_playlist("#EXTINF:1,\nretry\n")
        async with Stream(self.audio(playlist), retries=2) as stream:
            self.assertEqual(await stream.read(), b"RETRIED")
        self.assertEqual(self.http.hits["retry"], 2)

    async def test_interrupted_response_restarts_complete_fragment(self):
        playlist = fixture_playlist("#EXTINF:1,\ninterrupt\n")
        async with Stream(self.audio(playlist), retries=2) as stream:
            self.assertEqual(await stream.read(), b"COMPLETE")
        self.assertEqual(self.http.hits["interrupt"], 2)

    async def test_new_init_section_inserted_on_map_change(self):
        playlist = fixture_playlist(
            '#EXT-X-MAP:URI="init"\n'
            "#EXTINF:1,\na\n"
            '#EXT-X-MAP:URI="range"\n'
            "#EXTINF:1,\nb\n",
        )
        async with Stream(self.audio(playlist)) as stream:
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
            async with Stream(self.audio(playlist)) as stream:
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
            async with Stream(self.audio(playlist), concurrency=2) as stream:
                with self.assertRaises(OSError):
                    await stream.save(path)
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    async def test_exit_cancels_background_tasks(self):
        playlist = fixture_playlist("#EXTINF:1,\nslow\n#EXTINF:1,\nslow\n")
        async with Stream(self.audio(playlist)) as stream:
            task = asyncio.create_task(stream.__anext__())
            await asyncio.sleep(0.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with self.assertRaisesRegex(RuntimeError, "async with"):
            await stream.__anext__()

    async def test_context_required_and_single_use(self):
        source = self.audio(fixture_playlist("#EXTINF:1,\na\n"))
        instance = Stream(source)
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
            async with Stream(source, concurrency=0):
                pass
        playlist = fixture_playlist("#EXTINF:1,\nhttp://evil.test/file\n")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            async with Stream(self.audio(playlist)):
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
                    async with Stream(self.audio(text)):
                        pass

    async def test_save_and_read(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.m4a"
            async with Stream(
                self.audio(fixture_playlist("#EXTINF:1,\na\n"))
            ) as stream:
                returned = await stream.save(path)
            self.assertEqual(returned, path)
            self.assertEqual(path.read_bytes(), b"A" * 3000)

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
            async with Stream(
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
            async with Stream(
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
                async with Stream(self.audio(playlist), decryption_key=b"too short"):
                    pass
            async with Stream(
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
            async with Stream(self.audio(playlist), decryption_key=key) as stream:
                with self.assertRaisesRegex(OSError, "Only AES-CTR"):
                    await stream.read()
            split = original.index(b"moof") - 4
            self.http.fixtures["encrypted"] = original[split:]
            async with Stream(self.audio(playlist), decryption_key=key) as stream:
                with self.assertRaisesRegex(OSError, "initialization section"):
                    await stream.read()
