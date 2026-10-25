"""Local wrapper-v2 TCP fixture tests for native sample delivery."""

from __future__ import annotations

import asyncio
import socketserver
import threading
import unittest
from collections import Counter
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from gamdl.api.http import ApiHttpClient
from gamdl.api.wrapper import WrapperApi
from gamdl.drm import ContentKey, DRMInfo, Drm
from gamdl.media import (
    AudioStream,
    HlsSongStream,
    HlsType,
    SongCodec,
    StreamOrigin,
    VideoStream,
    WebPlaybackStream,
)
from gamdl.media.exceptions import StreamManifestError
from gamdl.stream import Streamer

from tests.test_cbcs_stream import (
    CT1,
    CT2,
    IV,
    KEY,
    KID,
    PREFETCH_CT,
    PT1,
    PT2,
    fixture,
    atom,
    be32,
    fullbox,
    mdats,
)
from tests.test_native_stream import HttpHandler, fixture_playlist, manifest_url


class WrapperHandler(socketserver.BaseRequestHandler):
    def recvall(self, count):
        buf = bytearray()
        while len(buf) < count:
            data = self.request.recv(count - len(buf))
            if not data:
                raise EOFError
            buf.extend(data)
        return bytes(buf)

    def handle(self):
        while True:
            try:
                header = self.recvall(16)
            except EOFError:
                return
            magic = int.from_bytes(header[0:4], "big")
            version = int.from_bytes(header[4:6], "big")
            kind = int.from_bytes(header[6:8], "big")
            request_id = int.from_bytes(header[8:12], "big")
            size = int.from_bytes(header[12:16], "big")
            if magic != 0x57563244 or version != 1 or size > 2_000_000:
                return
            body = self.recvall(size)
            if kind == 9:
                self.server.close_frames += 1
                return
            if kind != 1:
                return
            n_adam = int.from_bytes(body[0:2], "big")
            n_uri = int.from_bytes(body[2:4], "big")
            count = int.from_bytes(body[4:8], "big")
            pos = 8
            lengths = [
                int.from_bytes(body[pos + 4 * i : pos + 4 * i + 4], "big")
                for i in range(count)
            ]
            pos += 4 * count
            adam = body[pos : pos + n_adam]
            pos += n_adam
            skd = body[pos : pos + n_uri]
            pos += n_uri
            self.server.batches.append((adam, skd, count))
            iv = self.server.iv
            out = []
            for length in lengths:
                ciphertext = body[pos : pos + length]
                pos += length
                # Protocol mock: answer with the corresponding known NIST CBC
                # vector. AES itself is covered by native Rust crypto tests.
                if ciphertext != CT1 + CT2:
                    return
                first_block = bytes(
                    plain ^ original_iv ^ selected_iv
                    for plain, original_iv, selected_iv in zip(PT1, IV, iv)
                )
                out.append(first_block + PT2)
            if pos != len(body):
                return
            if self.server.invalid:
                response = bytes.fromhex("5756324400010003") + request_id.to_bytes(
                    4, "big"
                )
                payload = b"mock failure"
            else:
                if self.server.echo:
                    out = [CT1 + CT2 for _ in out]
                payload = count.to_bytes(4, "big")
                payload += b"".join(len(data).to_bytes(4, "big") for data in out)
                payload += b"".join(out)
                response = bytes.fromhex("5756324400010002") + request_id.to_bytes(
                    4, "big"
                )
            response += len(payload).to_bytes(4, "big")
            self.request.sendall(response + payload)


class WrapperStreamTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer

        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), HttpHandler)
        cls.http.lock = threading.Lock()
        cls.http.hits = Counter()
        cls.http.fixtures = {}
        cls.http_thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.http_thread.start()
        cls.url = f"http://127.0.0.1:{cls.http.server_port}"

        cls.wrapper = socketserver.ThreadingTCPServer(("127.0.0.1", 0), WrapperHandler)
        cls.wrapper.daemon_threads = True
        cls.wrapper.batches = []
        cls.wrapper.close_frames = 0
        cls.wrapper.iv = IV
        cls.wrapper.invalid = False
        cls.wrapper.echo = False
        cls.wrapper_thread = threading.Thread(
            target=cls.wrapper.serve_forever, daemon=True
        )
        cls.wrapper_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.http_thread.join()
        cls.wrapper.shutdown()
        cls.wrapper.server_close()
        cls.wrapper_thread.join()

    def setUp(self):
        self.http.fixtures.clear()
        self.http.hits.clear()
        self.wrapper.batches.clear()
        self.wrapper.iv = IV
        self.wrapper.invalid = False
        self.wrapper.echo = False
        self.wrapper.close_frames = 0
        self.api = WrapperApi(
            ApiHttpClient("wrapper"),
            "http://127.0.0.1",
            me={"version": "0.0.2", "auth": {"state": "authenticated"}},
            decrypt_host="127.0.0.1",
            decrypt_port=self.wrapper.server_address[1],
        )
        self.addAsyncCleanup(self.api.aclose)

    def source(
        self,
        *,
        origin=StreamOrigin.ASSET_STANDARD_HLS,
        hls_type=HlsType.STANDARD,
        fairplay_uri="skd://local/test",
    ):
        transport = Mock()
        key = (
            '#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.apple.streamingkeydelivery",'
            f'URI="{fairplay_uri}"\n'
            if fairplay_uri is not None
            else ""
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

    async def test_standard_asset_delivers_and_reassembles(self):
        init, frag, expected = fixture()
        self.http.fixtures.update({"init": init, "segment": frag})
        async with Streamer(
            self.source(),
            wrapper_api=self.api,
            chunk_size=1024,
        ) as stream:
            decoded = await stream.read()
        self.assertEqual(mdats(decoded[len(init) :]), expected)
        self.assertEqual(
            self.wrapper.batches, [(b"1559885421", b"skd://local/test", 2)]
        )
        self.assertNotIn(b"enca", decoded)

    async def test_enhanced_asset_request_with_standard_manifest_uses_wrapper_only(
        self,
    ):
        init, fragment, expected = fixture()
        self.http.fixtures.update({"init": init, "segment": fragment})
        # The enhancedHls endpoint can return a standard-HLS master. The
        # effective manifest layout, not the requested profile, is decisive.
        source = self.source(
            origin=StreamOrigin.ASSET_ENHANCED_HLS, hls_type=HlsType.STANDARD
        )
        async with Streamer(source, wrapper_api=self.api) as stream:
            decoded = await stream.read()
        self.assertEqual(mdats(decoded[len(init) :]), expected)
        self.assertEqual(sum(batch[2] for batch in self.wrapper.batches), 2)

    async def test_wrapper_failure_keeps_destination_unchanged(self):
        init, frag, _ = fixture()
        self.http.fixtures.update({"init": init, "segment": frag})
        self.wrapper.invalid = True
        with TemporaryDirectory() as directory:
            path = Path(directory) / "out.m4a"
            path.write_bytes(b"ORIGINAL")
            async with Streamer(
                self.source(),
                wrapper_api=self.api,
            ) as stream:
                with self.assertRaisesRegex(OSError, "rejected"):
                    await stream.save(path)
            self.assertEqual(path.read_bytes(), b"ORIGINAL")
            self.assertEqual(
                sorted(p.name for p in Path(directory).iterdir()), ["out.m4a"]
            )

    async def test_unchanged_ciphertext_from_wrapper_fails_closed(self):
        init, fragment, _ = fixture(video=True)
        self.http.fixtures.update({"init": init, "segment": fragment})
        source = self.source()
        source = VideoStream(
            codec="h264",
            origin=StreamOrigin.ITUNES_HLS,
            url=source.url,
            transport=source._transport,
            media_id="299199411",
        )
        self.wrapper.echo = True
        with TemporaryDirectory() as directory:
            path = Path(directory) / "video.m4v"
            path.write_bytes(b"PREEXISTING")
            async with Streamer(source, wrapper_api=self.api) as streamer:
                with self.assertRaisesRegex(OSError, "unchanged"):
                    await streamer.save(path)
            self.assertEqual(path.read_bytes(), b"PREEXISTING")

    async def test_enhanced_prefetch_then_wrapper(self):
        init, first, expected_first = fixture(second_description=True)
        _, second, expected_second = fixture(
            second_description=True, selected_description=2
        )
        first = first.replace(CT1 + CT2, PREFETCH_CT)
        # Only remote samples use the second sample description and IV.
        self.wrapper.iv = bytes(n ^ 0xFF for n in IV)
        self.http.fixtures.update({"init": init, "segment": first})
        transport = self.source(
            origin=StreamOrigin.WRAPPER_HLS, hls_type=HlsType.ENHANCED
        )
        transport._transport.get_manifest = AsyncMock(
            return_value=manifest_url(
                self.url,
                fixture_playlist(
                    "#EXTINF:1,\nsegment\n#EXTINF:1,\nsecond\n",
                    prefix=(
                        '#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.apple.streamingkeydelivery",'
                        'URI="skd://local/test"\n'
                        '#EXT-X-MAP:URI="init"\n'
                    ),
                ),
            )
        )
        self.http.fixtures["second"] = second
        async with Streamer(transport, wrapper_api=self.api) as stream:
            decoded = await stream.read()
        self.assertEqual(
            mdats(decoded[len(init) :]),
            expected_first + expected_second,
        )
        # Description 1 was decrypted locally; only 2 samples are remote.
        self.assertEqual(sum(b[2] for b in self.wrapper.batches), 2)

    async def test_large_fragment_is_split_into_128_sample_batches(self):
        init, fragment, expected_two = fixture()
        # Expand a two-sample fragment to 130 encrypted samples while
        # retaining valid trun sizes, senc subsample records and MP4 offsets.
        senc_at = fragment.index(b"senc") - 4
        senc_size = int.from_bytes(fragment[senc_at : senc_at + 4], "big")
        senc = fragment[senc_at : senc_at + senc_size]
        records = senc[16:] * 65
        new_senc = fullbox(b"senc", 0, 2, be32(130) + records)
        tfhd_at = fragment.index(b"tfhd") - 4
        tfhd_size = int.from_bytes(fragment[tfhd_at : tfhd_at + 4], "big")
        tfhd = fragment[tfhd_at : tfhd_at + tfhd_size]
        original_trun_at = fragment.index(b"trun") - 4
        trun_size = int.from_bytes(
            fragment[original_trun_at : original_trun_at + 4], "big"
        )
        trun = fragment[original_trun_at : original_trun_at + trun_size]
        original_sizes = trun[-8:]
        new_trun = fullbox(b"trun", 0, 0x201, be32(130) + be32(0) + original_sizes * 65)
        moof = atom(b"moof", atom(b"traf", tfhd + new_senc + new_trun))
        new_trun = fullbox(
            b"trun",
            0,
            0x201,
            be32(130) + be32(len(moof) + 8) + original_sizes * 65,
        )
        moof = atom(b"moof", atom(b"traf", tfhd + new_senc + new_trun))
        mdat_at = fragment.index(b"mdat") - 4
        payload = fragment[mdat_at + 8 :] * 65
        self.http.fixtures.update(
            {
                "init": init,
                "segment": moof + atom(b"mdat", payload),
            }
        )
        async with Streamer(self.source(), wrapper_api=self.api) as stream:
            decoded = await stream.read()
        self.assertEqual(mdats(decoded[len(init) :]), expected_two * 65)
        self.assertEqual([batch[2] for batch in self.wrapper.batches], [128, 2])

    async def test_multiple_truns_with_explicit_or_implicit_offsets(self):
        init, fragment, expected = fixture()
        senc_at = fragment.index(b"senc") - 4
        senc_size = int.from_bytes(fragment[senc_at : senc_at + 4], "big")
        senc = fragment[senc_at : senc_at + senc_size]
        tfhd_at = fragment.index(b"tfhd") - 4
        tfhd_size = int.from_bytes(fragment[tfhd_at : tfhd_at + 4], "big")
        tfhd = fragment[tfhd_at : tfhd_at + tfhd_size]
        trun_at = fragment.index(b"trun") - 4
        trun_size = int.from_bytes(fragment[trun_at : trun_at + 4], "big")
        trun = fragment[trun_at : trun_at + trun_size]
        first_size = int.from_bytes(trun[-8:-4], "big")
        second_size = int.from_bytes(trun[-4:], "big")
        mdat_at = fragment.index(b"mdat") - 4
        for implicit in (False, True):
            with self.subTest(implicit_second_run=implicit):
                first = fullbox(b"trun", 0, 0x201, be32(1) + be32(0) + be32(first_size))
                second = fullbox(
                    b"trun",
                    0,
                    0x200 if implicit else 0x201,
                    be32(1) + (b"" if implicit else be32(0)) + be32(second_size),
                )
                moof = atom(b"moof", atom(b"traf", tfhd + senc + first + second))
                first = fullbox(
                    b"trun",
                    0,
                    0x201,
                    be32(1) + be32(len(moof) + 8) + be32(first_size),
                )
                second = fullbox(
                    b"trun",
                    0,
                    0x200 if implicit else 0x201,
                    be32(1)
                    + (b"" if implicit else be32(len(moof) + 8 + first_size))
                    + be32(second_size),
                )
                moof = atom(b"moof", atom(b"traf", tfhd + senc + first + second))
                self.http.fixtures.update(
                    {
                        "init": init,
                        "segment": moof + fragment[mdat_at:],
                    }
                )
                async with Streamer(self.source(), wrapper_api=self.api) as stream:
                    downloaded = await stream.read()
                self.assertEqual(mdats(downloaded[len(init) :]), expected)

    async def test_wrapper_can_coexist_with_drm_and_ignores_ineligible_sources(self):
        source = self.source()
        drm = Drm.from_widevine(Mock())
        Streamer(source, wrapper_api=self.api, drm=drm)
        with self.assertRaises(TypeError):
            Streamer(source, fairplay_key="skd://test")
        with self.assertRaisesRegex(TypeError, "wrapper_api must be"):
            Streamer(source, wrapper_api=Mock())
        init, fragment, _ = fixture()
        self.http.fixtures.update({"init": init, "segment": fragment})
        source.origin = StreamOrigin.WEBPLAYBACK
        async with Streamer(source, wrapper_api=self.api) as stream:
            self.assertEqual(stream.decryption_method, "none")
            self.assertEqual(await stream.read(), init + fragment)
        source = self.source()
        source.media_id = None
        with self.assertRaisesRegex(ValueError, "media ID"):
            async with Streamer(source, wrapper_api=self.api):
                pass
        for cls in (VideoStream, WebPlaybackStream):
            bad = cls(
                codec="h264" if cls is VideoStream else SongCodec.AAC_WEB,
                origin=StreamOrigin.ASSET_STANDARD_HLS,
                url=f"{self.url}/hls.m3u8",
                transport=self.source()._transport,
                media_id="1559885421",
            )
            async with Streamer(bad, wrapper_api=self.api) as stream:
                self.assertEqual(stream.decryption_method, "none")
                self.assertEqual(await stream.read(), init + fragment)
        self.assertEqual(self.wrapper.batches, [])

    async def test_fairplay_uri_is_resolved_lazily_from_cached_manifest(self):
        init, fragment, expected = fixture()
        self.http.fixtures.update({"init": init, "segment": fragment})
        source = self.source()
        self.assertEqual(source._transport.get_manifest.await_count, 0)
        stream = Streamer(source, wrapper_api=self.api)
        self.assertEqual(source._transport.get_manifest.await_count, 0)
        async with stream:
            self.assertEqual(source._transport.get_manifest.await_count, 1)
            decoded = await stream.read()
        self.assertEqual(mdats(decoded[len(init) :]), expected)
        self.assertEqual(source._transport.get_manifest.await_count, 1)
        self.assertEqual(
            self.wrapper.batches, [(b"1559885421", b"skd://local/test", 2)]
        )

    async def test_missing_invalid_and_placeholder_fairplay_pass_through(self):
        init, fragment, _ = fixture()
        self.http.fixtures.update({"init": init, "segment": fragment})
        for uri in (
            None,
            "not-an-skd",
            "skd://",
            "skd://itunes.apple.com/P000000000/s1/e1",
        ):
            with self.subTest(uri=uri):
                source = self.source(fairplay_uri=uri)
                async with Streamer(source, wrapper_api=self.api) as stream:
                    self.assertEqual(stream.decryption_method, "none")
                    self.assertEqual(await stream.read(), init + fragment)
                self.assertEqual(source._transport.get_manifest.await_count, 1)
                self.assertEqual(self.wrapper.batches, [])

    async def test_wrapper_has_priority_over_matching_widevine(self):
        init, fragment, expected = fixture()
        self.http.fixtures.update({"init": init, "segment": fragment})
        source = self.source(origin=StreamOrigin.ASSET_ENHANCED_HLS)
        info = DRMInfo(
            fairplay_streaming="skd://local/test",
            widevine_pssh=b"pssh",
            widevine_uri="data:widevine",
            encryption_method="SAMPLE-AES",
        )
        source.get_drm_info = AsyncMock(return_value=info)
        drm = Drm.from_widevine(Mock())
        drm.get_keys = AsyncMock(side_effect=AssertionError("Should use wrapper"))
        async with Streamer(source, wrapper_api=self.api, drm=drm) as stream:
            self.assertEqual(stream.decryption_method, "wrapper")
            decoded = await stream.read()
        self.assertEqual(mdats(decoded[len(init) :]), expected)
        drm.get_keys.assert_not_awaited()
        self.assertEqual(
            self.wrapper.batches, [(b"1559885421", b"skd://local/test", 2)]
        )

    async def test_incompatible_fairplay_uses_widevine_when_present(self):
        init, fragment, expected = fixture()
        self.http.fixtures.update({"init": init, "segment": fragment})
        source = self.source(fairplay_uri=None)
        info = DRMInfo(
            widevine_pssh=b"pssh",
            widevine_uri="data:widevine",
            encryption_method="SAMPLE-AES",
        )
        source.get_drm_info = AsyncMock(return_value=info)
        drm = Drm.from_widevine(Mock())
        drm.get_keys = AsyncMock(return_value=ContentKey(KID, KEY))
        async with Streamer(source, wrapper_api=self.api, drm=drm) as stream:
            self.assertEqual(stream.decryption_method, "widevine")
            decoded = await stream.read()
        self.assertEqual(mdats(decoded[len(init) :]), expected)
        self.assertEqual(self.wrapper.batches, [])
        drm.get_keys.assert_awaited_once_with(
            media_id="1559885421", drm_info=info, is_library=False
        )

    async def test_explicit_key_overrides_wrapper_when_supplied(self):
        init, fragment, expected = fixture()
        self.http.fixtures.update({"init": init, "segment": fragment})
        async with Streamer(
            self.source(), wrapper_api=self.api, decryption_key=ContentKey(KID, KEY)
        ) as stream:
            self.assertEqual(stream.decryption_method, "explicit_key")
            decoded = await stream.read()
        self.assertEqual(mdats(decoded[len(init) :]), expected)
        self.assertEqual(self.wrapper.batches, [])

    async def test_music_video_audio_and_video_wrapper_delivery(self):
        for cls, is_video, origin in (
            (VideoStream, True, StreamOrigin.ITUNES_HLS),
            (AudioStream, False, StreamOrigin.ITUNES_HLS),
            (VideoStream, True, StreamOrigin.WRAPPER_HLS),
        ):
            with self.subTest(cls=cls, origin=origin):
                self.wrapper.batches.clear()
                init, segment, expected = fixture(video=is_video)
                self.http.fixtures.update({"init": init, "segment": segment})
                source = self.source()
                source = cls(
                    codec="h264" if is_video else SongCodec.AAC,
                    origin=origin,
                    url=source.url,
                    transport=source._transport,
                    media_id="299199411",
                )
                async with Streamer(source, wrapper_api=self.api) as streamer:
                    self.assertEqual(streamer.decryption_method, "wrapper")
                    downloaded = await streamer.read()
                self.assertNotIn(b"encv" if is_video else b"enca", downloaded)
                self.assertEqual(mdats(downloaded[len(init) :]), expected)
                self.assertEqual(
                    self.wrapper.batches,
                    [(b"299199411", b"skd://local/test", 2)],
                )
