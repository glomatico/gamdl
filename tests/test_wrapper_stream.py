"""Synthetic Wrapper-v2 -> Temari streaming contract tests.

The 3.x per-sample TCP fixture was removed: Wrapper only supplies a context,
and all sample decryption occurs inside the Rust native extension.
The synthetic context comes from the published glomatico/Temari emulator
fixture, not an Apple account or copyrighted media.
"""

from __future__ import annotations

import json
import threading
import unittest
from collections import Counter
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock

from gamdl.api.wrapper import WrapperApi
from gamdl.transport import ApiHttpClient
from gamdl.media import (
    HlsSongStream,
    HlsType,
    SongCodec,
    StreamOrigin,
)
from gamdl.stream import Streamer
from tests.test_cbcs_stream import CT1, CT2, PREFETCH_CT, fixture, mdats
from tests.test_native_stream import HttpHandler, fixture_playlist, manifest_url

SYNTHETIC_CONTEXT = json.loads(
    (Path(__file__).parent / "fixtures/temari-legacy-v1109.json").read_text()
)
# Independently recorded from the pinned synthetic Temari emulator context.
CIPHERTEXT = CT1 + CT2
PLAINTEXT = bytes.fromhex(
    "0cfa083acb2fcb6e3d0cada43a5d2e9478c9e72f5e5cdab888df5e393b495ade"
)


class TemariStreamTests(unittest.IsolatedAsyncioTestCase):
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
        self.http.fixtures.clear()
        self.http.hits.clear()
        self.api = WrapperApi(
            ApiHttpClient("wrapper"),
            "http://127.0.0.1:40020",
            session={"state": "authenticated"},
        )
        self.api.get_context = AsyncMock(return_value=SYNTHETIC_CONTEXT)
        self.addAsyncCleanup(self.api.aclose)

    def source(self, *,
               origin=StreamOrigin.ASSET_STANDARD_HLS,
               hls_type=HlsType.STANDARD,
               fairplay_uri="skd://local/test"):
        transport = Mock()
        key = (
            '#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.apple.streamingkeydelivery",'
            f'URI="{fairplay_uri}"\n'
            if fairplay_uri is not None else ""
        )
        transport.get_manifest = AsyncMock(
            return_value=manifest_url(
                self.url,
                fixture_playlist(
                    "#EXTINF:1,\nsegment\n",
                    prefix=key + '#EXT-X-MAP:URI="init"\n',
                ),
            )
        )
        return HlsSongStream(
            codec=SongCodec.AAC,
            origin=origin,
            url=f"{self.url}/hls.m3u8",
            transport=transport,
            media_id="1559885421",
            hls_type=hls_type,
        )

    async def test_context_decrypts_locally_without_sample_socket(self):
        init, fragment, _ = fixture()
        self.http.fixtures.update({"init": init, "segment": fragment})
        async with Streamer(self.source(), wrapper_api=self.api, chunk_size=1024) as stream:
            self.assertEqual(stream.decryption_method, "temari")
            decoded = await stream.read()
        expected = PLAINTEXT + b"tail" + b"ABCD" + PLAINTEXT + b"end"
        self.assertEqual(mdats(decoded[len(init):]), expected)
        self.assertNotIn(b"enca", decoded)
        self.api.get_context.assert_awaited_once_with("1559885421", "skd://local/test")

    async def test_context_failure_does_not_change_existing_file(self):
        init, fragment, _ = fixture()
        self.http.fixtures.update({"init": init, "segment": fragment})
        self.api.get_context.side_effect = RuntimeError("synthetic context failure")
        with TemporaryDirectory() as folder:
            path = Path(folder) / "song.m4a"
            path.write_bytes(b"ORIGINAL")
            with self.assertRaisesRegex(RuntimeError, "synthetic context failure"):
                async with Streamer(self.source(), wrapper_api=self.api) as stream:
                    await stream.save(path)
            self.assertEqual(path.read_bytes(), b"ORIGINAL")

    async def test_unknown_profile_rejected_before_downloading(self):
        context = dict(SYNTHETIC_CONTEXT, profile="unsupported-version")
        self.api.get_context.return_value = context
        init, fragment, _ = fixture()
        self.http.fixtures.update({"init": init, "segment": fragment})
        with self.assertRaisesRegex(ValueError, "Temari"):
            async with Streamer(self.source(), wrapper_api=self.api):
                pass

    async def test_clear_hls_does_not_request_context(self):
        init, fragment, _ = fixture()
        self.http.fixtures.update({"init": init, "segment": fragment})
        async with Streamer(self.source(fairplay_uri=None), wrapper_api=self.api) as stream:
            self.assertEqual(stream.decryption_method, "none")
        self.api.get_context.assert_not_awaited()

    async def test_enhanced_prefetch_is_decrypted_locally(self):
        init, first, first_expected = fixture(second_description=True)
        _, second, _ = fixture(second_description=True, selected_description=2)
        first = first.replace(CIPHERTEXT, PREFETCH_CT)
        self.http.fixtures.update({"init": init, "segment": first, "second": second})
        source = self.source(origin=StreamOrigin.WRAPPER_HLS, hls_type=HlsType.ENHANCED)
        source._transport.get_manifest = AsyncMock(
            return_value=manifest_url(
                self.url,
                fixture_playlist(
                    "#EXTINF:1,\nsegment\n#EXTINF:1,\nsecond\n",
                    prefix=(
                        '#EXT-X-KEY:METHOD=SAMPLE-AES,'
                        'KEYFORMAT="com.apple.streamingkeydelivery",'
                        'URI="skd://local/test"\n'
                        '#EXT-X-MAP:URI="init"\n'
                    ),
                ),
            )
        )
        async with Streamer(source, wrapper_api=self.api) as stream:
            decoded = await stream.read()
        temari_expected = PLAINTEXT + b"tail" + b"ABCD" + PLAINTEXT + b"end"
        self.assertEqual(
            mdats(decoded[len(init):]),
            first_expected + temari_expected,
        )
        self.api.get_context.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
