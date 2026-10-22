"""Local HTTP fixtures exercise native Tokio/reqwest, not HTTPX mocking."""

import asyncio
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
