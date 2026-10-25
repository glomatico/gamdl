"""Synthetic CBCS fragmented-MP4 fixtures with known NIST AES-CBC vectors.

The fixtures use valid MP4 box framing and the same tenc/senc layout as Apple's
SAMPLE-AES/CBCS streams. No DRM license, credentials, or remote Apple data.
"""

from __future__ import annotations

import asyncio
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import AsyncMock, Mock

from gamdl.drm import ContentKey, DRMInfo, Drm, DrmBackend, DrmLicenseError
from gamdl.media import (
    AudioStream,
    HlsSongStream,
    HlsType,
    SongCodec,
    StreamOrigin,
    VideoStream,
)
from gamdl.stream import Streamer
from tests.test_native_stream import HttpHandler, fixture_playlist, manifest_url

KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
KID = bytes.fromhex("101112131415161718191a1b1c1d1e1f")
IV = bytes(range(16))
CT1 = bytes.fromhex("7649abac8119b246cee98e9b12e9197d")
CT2 = bytes.fromhex("5086cb9b507219ee95db113a917678b2")
PT1 = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
PT2 = bytes.fromhex("ae2d8a571e03ac9c9eb76fac45af8e51")
SKIP = b"clear skip block"
# Ciphertext of PT1+PT2 encrypted using Gamdl 3.x's fixed enhanced-HLS
# prefetch key and IV. Fixed test-vector bytes, not a second copy of the key.
PREFETCH_CT = bytes.fromhex(
    "6ab97d9019bac24e943afbf9fca8d6414032cf5f6c84c64667e84246a36b2800"
)


def be32(i: int) -> bytes:
    return i.to_bytes(4, "big")


def atom(kind: bytes, body: bytes) -> bytes:
    return be32(len(body) + 8) + kind + body


def fullbox(kind: bytes, version: int, flags: int, body: bytes) -> bytes:
    return atom(kind, bytes([version]) + flags.to_bytes(3, "big") + body)


def fixture(
    *,
    video=False,
    pattern=0,
    per_sample_iv=False,
    corrupt_iv=False,
    second_description=False,
    selected_description=1,
    split_unpatterned_cbcs=False,
):
    """Construct init + fragment and known plaintext for two CBCS samples."""
    sample_encrypted = CT1 + (SKIP if pattern else b"") + CT2 + b"tail"
    sample_plain = PT1 + (SKIP if pattern else b"") + PT2 + b"tail"
    other_encrypted = b"ABCD" + CT1 + (SKIP if pattern else b"") + CT2 + b"end"
    other_plain = b"ABCD" + PT1 + (SKIP if pattern else b"") + PT2 + b"end"

    if split_unpatterned_cbcs:
        assert not pattern
        gap = b"GAP"
        sample_encrypted = CT1 + CT2[:5] + gap + CT2[5:] + b"tail"
        sample_plain = PT1 + PT2[:5] + gap + PT2[5:] + b"tail"
        other_encrypted = b"ABCD" + CT1 + CT2[:5] + gap + CT2[5:] + b"end"
        other_plain = b"ABCD" + PT1 + PT2[:5] + gap + PT2[5:] + b"end"

    iv_size = 16 if per_sample_iv else 0
    tenc_body = bytes([pattern, 0, 1, iv_size]) + KID
    if not per_sample_iv:
        tenc_body += bytes([8 if corrupt_iv else 16]) + IV

    def sample_description(tenc_payload):
        sinf = atom(
            b"sinf",
            atom(b"frma", b"avc1" if video else b"mp4a")
            + fullbox(b"schm", 0, 0, b"cbcs" + be32(0x10000))
            + atom(b"schi", fullbox(b"tenc", 1, 0, tenc_payload)),
        )
        return atom(
            b"encv" if video else b"enca",
            bytes(78 if video else 28) + sinf,
        )

    descriptions = [sample_description(tenc_body)]
    if second_description:
        assert not per_sample_iv
        different_iv = bytes(value ^ 0xFF for value in IV)
        descriptions.append(
            sample_description(bytes([pattern, 0, 1, 0]) + KID + b"\x10" + different_iv)
        )
    if selected_description == 2:
        assert second_description
        # The ciphertext was generated with IV, so using the alternate IV
        # changes only the first plaintext block in each sample.
        changed_pt1 = bytes(value ^ 0xFF for value in PT1)
        sample_plain = sample_plain.replace(PT1, changed_pt1)
        other_plain = other_plain.replace(PT1, changed_pt1)
    stsd = fullbox(
        b"stsd",
        0,
        0,
        be32(len(descriptions)) + b"".join(descriptions),
    )
    stbl = atom(b"stbl", stsd)
    minf = atom(b"minf", stbl)
    hdlr = fullbox(b"hdlr", 0, 0, be32(0) + (b"vide" if video else b"soun") + be32(0))
    mdia = atom(b"mdia", hdlr + minf)
    tkhd = fullbox(b"tkhd", 0, 0, be32(0) + be32(0) + be32(1) + be32(0))
    trak = atom(b"trak", tkhd + mdia)
    trex = fullbox(b"trex", 0, 0, be32(1) + be32(1) + be32(1024) + be32(0) + be32(0))
    init = atom(b"ftyp", b"isom" + be32(0) + b"isom") + atom(
        b"moov", trak + atom(b"mvex", trex)
    )

    samples = (sample_encrypted, other_encrypted)
    iv = IV if per_sample_iv else b""
    if split_unpatterned_cbcs:
        entries = (
            ((0, 21), (3, 11)),
            ((4, 21), (3, 11)),
        )
        senc_body = be32(2)
        for ranges in entries:
            senc_body += iv + len(ranges).to_bytes(2, "big")
            for clear, encrypted in ranges:
                senc_body += clear.to_bytes(2, "big") + be32(encrypted)
    else:
        senc_body = (
            be32(2)
            + iv
            + (1).to_bytes(2, "big")
            + (0).to_bytes(2, "big")
            + be32(len(samples[0]))
            + iv
            + (2).to_bytes(2, "big")
            + (4).to_bytes(2, "big")
            + be32(len(samples[1]) - 7)
            + (3).to_bytes(2, "big")
            + be32(0)
        )
    senc = fullbox(b"senc", 0, 2, senc_body)
    tfhd = fullbox(
        b"tfhd",
        0,
        0x020002 if selected_description == 2 else 0x020000,
        be32(1) + (be32(2) if selected_description == 2 else b""),
    )
    trun_zero = fullbox(
        b"trun", 0, 0x201, be32(2) + be32(0) + b"".join(be32(len(s)) for s in samples)
    )
    traf_zero = atom(b"traf", tfhd + senc + trun_zero)
    moof_len = len(atom(b"moof", traf_zero))
    trun = fullbox(
        b"trun",
        0,
        0x201,
        be32(2) + be32(moof_len + 8) + b"".join(be32(len(s)) for s in samples),
    )
    traf = atom(b"traf", tfhd + senc + trun)
    fragment = atom(b"moof", traf) + atom(b"mdat", b"".join(samples))
    assert len(fragment) - len(b"".join(samples)) == moof_len + 8
    return init, fragment, sample_plain + other_plain


def mdats(data: bytes) -> bytes:
    """Return plaintext data from all mdat boxes in one fMP4 fragment."""
    result = bytearray()
    at = 0
    while at + 8 <= len(data):
        length = int.from_bytes(data[at : at + 4], "big")
        typ = data[at + 4 : at + 8]
        if length < 8 or at + length > len(data):
            raise AssertionError("bad atom framing")
        if typ == b"mdat":
            result.extend(data[at + 8 : at + length])
        at += length
    assert at == len(data)
    return bytes(result)


def captioned_video_fixture():
    """Encrypted HLS video with a separate clear CEA-608 track in its moof.

    Some Apple Music renditions also contain a caption-only moof. The other
    traf must remain untouched while the primary video sample is decrypted.
    """
    init, fragment, plaintext = fixture(video=True)
    ftyp_end = int.from_bytes(init[:4], "big")
    assert init[ftyp_end + 4 : ftyp_end + 8] == b"moov"
    caption = atom(
        b"trak",
        fullbox(b"tkhd", 0, 0, be32(0) + be32(0) + be32(2) + be32(0))
        + atom(
            b"mdia",
            fullbox(b"hdlr", 0, 0, be32(0) + b"clcp" + be32(0))
            + atom(
                b"minf",
                atom(b"stbl", fullbox(b"stsd", 0, 0, be32(1) + atom(b"c608", b""))),
            ),
        ),
    )
    init = init[:ftyp_end] + atom(b"moov", init[ftyp_end + 8 :] + caption)

    original_moof_end = int.from_bytes(fragment[:4], "big")
    original_data = fragment[original_moof_end + 8 :]
    caption_data = b"captions in same moof"
    auxiliary_traf = atom(
        b"traf",
        fullbox(b"tfhd", 0, 0x020000, be32(2))
        + fullbox(b"trun", 0, 0x201, be32(1) + be32(0) + be32(len(caption_data))),
    )
    moof = bytearray(atom(b"moof", fragment[8:original_moof_end] + auxiliary_traf))
    video_trun = moof.index(b"trun")
    caption_trun = moof.rindex(b"trun")
    moof[video_trun + 12 : video_trun + 16] = be32(len(moof) + 8)
    moof[caption_trun + 12 : caption_trun + 16] = be32(
        len(moof) + 8 + len(original_data)
    )
    fragment = bytes(moof) + atom(b"mdat", original_data + caption_data)

    standalone_caption = b"captions only"
    caption_only_moof = bytearray(
        atom(
            b"moof",
            atom(
                b"traf",
                fullbox(b"tfhd", 0, 0x020000, be32(2))
                + fullbox(
                    b"trun", 0, 0x201, be32(1) + be32(0) + be32(len(standalone_caption))
                ),
            ),
        )
    )
    caption_only_trun = caption_only_moof.index(b"trun")
    caption_only_moof[caption_only_trun + 12 : caption_only_trun + 16] = be32(
        len(caption_only_moof) + 8
    )
    fragment += bytes(caption_only_moof) + atom(b"mdat", standalone_caption)
    return init, fragment, plaintext + caption_data + standalone_caption


class CbcsStreamTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), HttpHandler)
        cls.http.lock = threading.Lock()
        from collections import Counter

        cls.http.hits = Counter()
        cls.http.fixtures = {}
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.http.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()

    def setUp(self):
        self.http.hits.clear()
        self.http.fixtures.clear()

    def source(
        self,
        *,
        video=False,
        segments="cbcs-fragment",
        origin=StreamOrigin.ASSET_STANDARD_HLS,
    ):
        transport = Mock()
        transport.get_manifest = AsyncMock(
            return_value=manifest_url(
                self.base,
                fixture_playlist(
                    "".join(f"#EXTINF:1,\n{name}\n" for name in segments.split(",")),
                    prefix='#EXT-X-MAP:URI="cbcs-init"\n',
                ),
            )
        )
        args = {
            "codec": "h264" if video else SongCodec.AAC,
            "origin": origin,
            "url": f"{self.base}/hls.m3u8",
            "transport": transport,
        }
        return VideoStream(**args) if video else AudioStream(**args)

    async def check_fixture(self, *, video=False, pattern=0, per_sample_iv=False):
        init, fragment, expected = fixture(
            video=video, pattern=pattern, per_sample_iv=per_sample_iv
        )
        self.http.fixtures["cbcs-init"] = init
        self.http.fixtures["cbcs-fragment"] = fragment
        async with Streamer(
            self.source(video=video), decryption_key=ContentKey(KID, KEY)
        ) as stream:
            decoded = await stream.read()
        self.assertEqual(mdats(decoded[len(init) :]), expected)
        self.assertIn(b"free", decoded)
        self.assertNotIn(b"enca" if not video else b"encv", decoded)
        self.assertEqual(self.http.hits["cbcs-init"], 1)
        self.assertEqual(self.http.hits["cbcs-fragment"], 1)

    async def test_unpatterned_constant_iv_cbcs_audio(self):
        await self.check_fixture()

    async def test_cbcs_decrypts_blocks_split_across_subsamples_like_gamdl3(self):
        for video in (False, True):
            with self.subTest(video=video):
                init, fragment, expected = fixture(
                    video=video, split_unpatterned_cbcs=True
                )
                self.http.fixtures.update(
                    {"cbcs-init": init, "cbcs-fragment": fragment}
                )
                async with Streamer(
                    self.source(video=video), decryption_key=ContentKey(KID, KEY)
                ) as streamer:
                    decoded = await streamer.read()
                self.assertEqual(mdats(decoded[len(init) :]), expected)

    async def test_patterned_cbcs_video(self):
        await self.check_fixture(video=True, pattern=0x11)

    async def test_music_video_with_clear_caption_track_in_same_fragment(self):
        init, fragment, expected = captioned_video_fixture()
        self.http.fixtures.update({"cbcs-init": init, "cbcs-fragment": fragment})
        source = self.source(video=True, origin=StreamOrigin.ITUNES_HLS)
        async with Streamer(source, decryption_key=ContentKey(KID, KEY)) as streamer:
            decrypted = await streamer.read()
        self.assertEqual(mdats(decrypted[len(init) :]), expected)
        self.assertIn(b"clcp", decrypted)
        self.assertIn(b"captions only", decrypted)

    async def test_unrecognized_auxiliary_track_is_rejected(self):
        init, fragment, _ = captioned_video_fixture()
        fragment = bytearray(fragment)
        tfhds = []
        position = 0
        while (pos := fragment.find(b"tfhd", position)) != -1:
            tfhds.append(pos)
            position = pos + 4
        self.assertGreaterEqual(len(tfhds), 2)
        fragment[tfhds[1] + 8 : tfhds[1] + 12] = be32(3)
        self.http.fixtures.update({"cbcs-init": init, "cbcs-fragment": bytes(fragment)})
        source = self.source(video=True)
        async with Streamer(source, decryption_key=ContentKey(KID, KEY)) as streamer:
            with self.assertRaisesRegex(OSError, "Unexpected non-caption track"):
                await streamer.read()

    async def test_cbcs_with_per_sample_iv(self):
        await self.check_fixture(per_sample_iv=True)

    async def test_multiple_sample_descriptions_select_distinct_constant_ivs(self):
        init, first, expected_first = fixture(second_description=True)
        other_init, second, expected_second = fixture(
            second_description=True,
            selected_description=2,
        )
        self.assertEqual(init, other_init)
        self.http.fixtures.update(
            {
                "cbcs-init": init,
                "cbcs-fragment": first,
                "cbcs-next": second,
            }
        )
        async with Streamer(
            self.source(segments="cbcs-fragment,cbcs-next"),
            decryption_key=ContentKey(KID, KEY),
        ) as stream:
            output = await stream.read()
        self.assertEqual(mdats(output[len(init) :]), expected_first + expected_second)

    async def test_enhanced_song_cbcs_uses_prefetch_then_license_key(self):
        init, initial_fragment, expected_initial = fixture(second_description=True)
        _, later_fragment, expected_later = fixture(
            second_description=True, selected_description=2
        )
        # Description 1 uses the fixed prefetch key, not the license key.
        initial_fragment = initial_fragment.replace(CT1 + CT2, PREFETCH_CT)
        self.http.fixtures.update(
            {
                "cbcs-init": init,
                "cbcs-fragment": initial_fragment,
                "cbcs-next": later_fragment,
            }
        )
        # Apple's enhanced-HLS license KID can differ from its tenc KID.
        license_key = ContentKey(b"L" * 16, KEY)
        audio = self.source(
            segments="cbcs-fragment,cbcs-next",
            origin=StreamOrigin.ASSET_ENHANCED_HLS,
        )
        async with Streamer(audio, decryption_key=license_key) as stream:
            output = await stream.read()
        self.assertEqual(mdats(output[len(init) :]), expected_initial + expected_later)

        # Do NOT accept that mismatched KID for ordinary CBCS streams.
        standard_audio = self.source(segments="cbcs-fragment,cbcs-next")
        async with Streamer(standard_audio, decryption_key=license_key) as stream:
            with self.assertRaisesRegex(OSError, "key ID"):
                await stream.read()

        # Enhanced-HLS VIDEO must not use the song-specific prefetch key.
        video_init, video_fragment, _ = fixture(video=True, second_description=True)
        self.http.fixtures.update(
            {
                "cbcs-init": video_init,
                "cbcs-fragment": video_fragment,
            }
        )
        video = self.source(video=True, origin=StreamOrigin.ASSET_ENHANCED_HLS)
        async with Streamer(video, decryption_key=license_key) as stream:
            with self.assertRaisesRegex(OSError, "key ID"):
                await stream.read()

    async def test_invalid_constant_iv_fails_and_prevents_output(self):
        init, fragment, _ = fixture(corrupt_iv=True)
        self.http.fixtures.update({"cbcs-init": init, "cbcs-fragment": fragment})
        async with Streamer(
            self.source(), decryption_key=ContentKey(KID, KEY)
        ) as stream:
            with self.assertRaisesRegex(OSError, "Unsupported fragmented MP4"):
                await stream.read()

    async def test_wrong_content_key_id_rejected(self):
        init, fragment, _ = fixture()
        self.http.fixtures.update({"cbcs-init": init, "cbcs-fragment": fragment})
        async with Streamer(
            self.source(), decryption_key=ContentKey(b"0" * 16, KEY)
        ) as stream:
            with self.assertRaisesRegex(OSError, "key ID"):
                await stream.read()

    async def test_asset_standard_song_accepts_licensed_cbcs_key_with_other_kid(self):
        init, fragment, expected = fixture()
        self.http.fixtures.update({"cbcs-init": init, "cbcs-fragment": fragment})
        source = self.source()
        apple_source = HlsSongStream(
            codec=SongCodec.AAC,
            origin=StreamOrigin.ASSET_STANDARD_HLS,
            url=source.url,
            transport=source._transport,
            hls_type=HlsType.STANDARD,
        )
        async with Streamer(
            apple_source, decryption_key=ContentKey(b"X" * 16, KEY)
        ) as stream:
            decoded = await stream.read()
        self.assertEqual(mdats(decoded[len(init) :]), expected)
        # Ordinary stream types must still verify their declared KID.
        async with Streamer(
            source, decryption_key=ContentKey(b"X" * 16, KEY)
        ) as stream:
            with self.assertRaisesRegex(OSError, "key ID"):
                await stream.read()

    async def test_different_key_ids_across_descriptions_are_rejected(self):
        init, fragment, _ = fixture(second_description=True)
        init = bytearray(init)
        first = init.index(b"tenc")
        second = init.index(b"tenc", first + 4)
        init[second + 12 : second + 28] = b"0" * 16
        self.http.fixtures.update(
            {
                "cbcs-init": bytes(init),
                "cbcs-fragment": fragment,
            }
        )
        async with Streamer(self.source(), decryption_key=KEY) as stream:
            with self.assertRaisesRegex(OSError, "Multiple content key IDs"):
                await stream.read()

    async def test_sample_group_key_rotation_is_rejected(self):
        init, fragment, _ = fixture()
        fragment = bytearray(fragment)
        pos = fragment.index(b"senc")
        fragment[pos : pos + 4] = b"sbgp"
        fragment[pos + 8 : pos + 12] = b"seig"
        self.http.fixtures.update(
            {
                "cbcs-init": init,
                "cbcs-fragment": bytes(fragment),
            }
        )
        async with Streamer(self.source(), decryption_key=KEY) as stream:
            with self.assertRaisesRegex(OSError, "Sample-group encryption overrides"):
                await stream.read()

    async def test_raw_copies_cbcs_without_decryption(self):
        init, fragment, _ = fixture()
        self.http.fixtures.update({"cbcs-init": init, "cbcs-fragment": fragment})
        async with Streamer(self.source()) as stream:
            self.assertEqual(stream.decryption_method, "none")
            self.assertEqual(await stream.read(), init + fragment)

    async def test_auto_drm_decrypts_audio_and_video_with_matching_backend(self):
        for backend, video in (
            (DrmBackend.WIDEVINE, False),
            (DrmBackend.PLAYREADY, False),
            (DrmBackend.WIDEVINE, True),
        ):
            with self.subTest(backend=backend, video=video):
                init, fragment, expected = fixture(video=video)
                self.http.fixtures.update(
                    {"cbcs-init": init, "cbcs-fragment": fragment}
                )
                source = self.source(video=video)
                source.media_id = "12345"
                info = DRMInfo(
                    widevine_pssh=b"widevine-pssh",
                    widevine_uri="data:widevine",
                    playready_pssh=b"playready-pro",
                    playready_uri="data:playready",
                    encryption_method="SAMPLE-AES",
                )
                source.get_drm_info = AsyncMock(return_value=info)
                drm = (
                    Drm.from_widevine(Mock())
                    if backend == DrmBackend.WIDEVINE
                    else Drm.from_playready(Mock(), cdm=Mock())
                )
                drm.get_keys = AsyncMock(return_value=ContentKey(KID, KEY))
                stream = Streamer(source, drm=drm)
                drm.get_keys.assert_not_awaited()
                with self.assertRaisesRegex(RuntimeError, "after entering"):
                    _ = stream.decryption_method
                async with stream:
                    self.assertEqual(stream.decryption_method, backend.value)
                    decoded = await stream.read()
                self.assertEqual(stream.decryption_method, backend.value)
                self.assertEqual(mdats(decoded[len(init) :]), expected)
                self.assertIn(b"free", decoded)
                drm.get_keys.assert_awaited_once_with(
                    media_id="12345", drm_info=info, is_library=False
                )
                source._transport.get_manifest.assert_awaited_once()

    async def test_apple_cbcs_video_accepts_zero_kid_placeholder_only(self):
        init, fragment, expected = fixture(video=True)
        init = bytearray(init)
        at = init.index(b"tenc")
        init[at + 12 : at + 28] = bytes(16)
        self.http.fixtures.update({"cbcs-init": bytes(init), "cbcs-fragment": fragment})

        for origin in (StreamOrigin.ITUNES_HLS, StreamOrigin.WRAPPER_HLS):
            with self.subTest(origin=origin):
                video = self.source(video=True, origin=origin)
                async with Streamer(
                    video, decryption_key=ContentKey(KID, KEY)
                ) as streamer:
                    decoded = await streamer.read()
                self.assertEqual(mdats(decoded[len(init) :]), expected)

        # Apple audio, unrelated sources and nonzero KIDs retain validation.
        for origin in (StreamOrigin.ASSET_STANDARD_HLS, StreamOrigin.WEBPLAYBACK):
            with self.subTest(origin=origin):
                video = self.source(video=True, origin=origin)
                async with Streamer(
                    video, decryption_key=ContentKey(KID, KEY)
                ) as streamer:
                    with self.assertRaisesRegex(OSError, "key ID"):
                        await streamer.read()

        nonzero_init = bytearray(init)
        nonzero_init[at + 12 : at + 28] = b"P" * 16
        self.http.fixtures["cbcs-init"] = bytes(nonzero_init)
        video = self.source(video=True, origin=StreamOrigin.ITUNES_HLS)
        async with Streamer(video, decryption_key=ContentKey(KID, KEY)) as streamer:
            with self.assertRaisesRegex(OSError, "key ID"):
                await streamer.read()

    async def test_music_video_audio_cbcs_accepts_only_zero_kid_placeholder(self):
        init, fragment, expected = fixture()
        init = bytearray(init)
        at = init.index(b"tenc")
        init[at + 12 : at + 28] = bytes(16)
        self.http.fixtures.update({"cbcs-init": bytes(init), "cbcs-fragment": fragment})
        for origin in (StreamOrigin.ITUNES_HLS, StreamOrigin.WRAPPER_HLS):
            with self.subTest(origin=origin):
                source = self.source(origin=origin)
                async with Streamer(
                    source, decryption_key=ContentKey(KID, KEY)
                ) as streamer:
                    decoded = await streamer.read()
                self.assertEqual(mdats(decoded[len(init) :]), expected)
        for origin in (StreamOrigin.WEBPLAYBACK, StreamOrigin.ASSET_STANDARD_HLS):
            with self.subTest(origin=origin):
                source = self.source(origin=origin)
                async with Streamer(
                    source, decryption_key=ContentKey(KID, KEY)
                ) as streamer:
                    with self.assertRaisesRegex(OSError, "key ID"):
                        await streamer.read()

        ordinary_song = self.source(origin=StreamOrigin.WRAPPER_HLS)
        ordinary_song = HlsSongStream(
            codec=SongCodec.AAC,
            origin=StreamOrigin.WRAPPER_HLS,
            url=ordinary_song.url,
            transport=ordinary_song._transport,
            hls_type=HlsType.STANDARD,
        )
        async with Streamer(
            ordinary_song, decryption_key=ContentKey(KID, KEY)
        ) as streamer:
            with self.assertRaisesRegex(OSError, "key ID"):
                await streamer.read()

    async def test_unavailable_drm_signaling_streams_original_encrypted_bytes(self):
        init, fragment, _ = fixture()
        self.http.fixtures.update({"cbcs-init": init, "cbcs-fragment": fragment})
        for info in (
            DRMInfo(),
            DRMInfo(encryption_method="SAMPLE-AES"),
            DRMInfo(
                fairplay_streaming="skd://only-fairplay",
                encryption_method="SAMPLE-AES",
            ),
            DRMInfo(
                playready_pssh=b"playready-only",
                playready_uri="data:playready",
                encryption_method="SAMPLE-AES",
            ),
        ):
            with self.subTest(info=info):
                source = self.source()
                source.get_drm_info = AsyncMock(return_value=info)
                drm = Drm.from_widevine(Mock())
                drm.get_keys = AsyncMock(side_effect=AssertionError("No license"))
                async with Streamer(source, drm=drm) as stream:
                    self.assertEqual(stream.decryption_method, "none")
                    self.assertEqual(await stream.read(), init + fragment)
                drm.get_keys.assert_not_awaited()

    async def test_failed_license_is_not_downgraded_to_encrypted_output(self):
        source = self.source()
        source.media_id = "12345"
        source.get_drm_info = AsyncMock(
            return_value=DRMInfo(widevine_pssh=b"data", widevine_uri="data:widevine")
        )
        drm = Drm.from_widevine(Mock())
        drm.get_keys = AsyncMock(side_effect=DrmLicenseError("License failed"))
        stream = Streamer(source, drm=drm)
        with self.assertRaisesRegex(DrmLicenseError, "License failed"):
            async with stream:
                self.fail("License error must abort before downloading")
        self.assertEqual(self.http.hits["cbcs-fragment"], 0)
        drm.get_keys.assert_awaited_once()

        # Once a matching backend attempted licensing, a missing key is also
        # an error, not permission to return encrypted media silently.
        drm.get_keys = AsyncMock(return_value=None)
        with self.assertRaisesRegex(DrmLicenseError, "no content key"):
            async with Streamer(source, drm=drm):
                pass
        self.assertEqual(self.http.hits["cbcs-fragment"], 0)

    async def test_library_drm_flag_and_missing_media_id(self):
        source = self.source(origin=StreamOrigin.LIBRARY_WEBPLAYBACK)
        source.media_id = "i.12345"
        info = DRMInfo(widevine_pssh=b"pssh", widevine_uri="data:widevine")
        source.get_drm_info = AsyncMock(return_value=info)
        drm = Drm.from_widevine(Mock())
        drm.get_keys = AsyncMock(return_value=ContentKey(KID, KEY))
        init, fragment, expected = fixture()
        self.http.fixtures.update({"cbcs-init": init, "cbcs-fragment": fragment})
        async with Streamer(source, drm=drm) as stream:
            self.assertEqual(mdats((await stream.read())[len(init) :]), expected)
        drm.get_keys.assert_awaited_once_with(
            media_id="i.12345", drm_info=info, is_library=True
        )
        source = self.source()
        source.get_drm_info = AsyncMock(return_value=info)
        with self.assertRaisesRegex(ValueError, "media ID"):
            async with Streamer(source, drm=drm):
                pass

    async def test_explicit_key_overrides_automatic_selection_and_validates_args(self):
        source = self.source()
        drm = Drm.from_widevine(Mock())
        with self.assertRaisesRegex(ValueError, "either decryption_key or drm"):
            Streamer(source, drm=drm, decryption_key=KEY)
        with self.assertRaisesRegex(TypeError, "drm must be"):
            Streamer(source, drm=Mock())
        init, fragment, expected = fixture()
        self.http.fixtures.update({"cbcs-init": init, "cbcs-fragment": fragment})
        async with Streamer(source, decryption_key=ContentKey(KID, KEY)) as stream:
            self.assertEqual(stream.decryption_method, "explicit_key")
            self.assertEqual(mdats((await stream.read())[len(init) :]), expected)
