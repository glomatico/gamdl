"""Offline DRM license contracts; provisioned devices and real keys not used."""

import base64
import importlib.util
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace
from uuid import UUID

import httpx

from gamdl.media import (
    ContentKey,
    DrmBackend,
    DrmConfigurationError,
    DrmLicenseError,
    DrmMetadataError,
    GamdlMediaError,
    Song,
    SongCodec,
    StreamOrigin,
    WebPlaybackStream,
)
from gamdl.media.drm import _playready_header, _widevine_pssh
from gamdl.media.stream import StreamTransport

KID = bytes.fromhex("00112233445566778899aabbccddeeff")
SECRET = bytes.fromhex("11223344556677889900aabbccddeeff")
KEY_URI = "data:;base64," + base64.b64encode(KID).decode("ascii")
URL = "https://cdn.example.test/playlist.m3u8"


class PSSH:
    """Sentinel for testing CDM I/O independently of optional PSSH parsers."""


class WRMHeader:
    """Sentinel for testing CDM I/O independently of optional XML parsers."""


def playlist(keyformat=None, method="ISO-23001-7", raw=KEY_URI):
    extra = f',KEYFORMAT="{keyformat}"' if keyformat else ""
    return (
        "#EXTM3U\n"
        f'#EXT-X-KEY:METHOD={method},URI="{raw}"{extra}\n'
        "#EXTINF:2,\nsegment.m4s\n"
    )


class FakeCdm:
    def __init__(self, *, backend=DrmBackend.WIDEVINE, fail=None, keys=None):
        self.backend = backend
        self.fail = fail
        self.session = b"fake-session"
        self.events = []
        self.challenge_input = None
        if keys is None:
            if backend == DrmBackend.WIDEVINE:
                keys = [
                    Mock(type="SIGNING", kid=UUID(bytes=KID), key=SECRET),
                    Mock(type="CONTENT", kid=UUID(bytes=KID), key=SECRET),
                ]
            else:
                keys = [
                    Mock(
                        key_id=UUID(bytes=KID),
                        key=SECRET,
                        key_type=SimpleNamespace(name="AES_128_CTR"),
                    )
                ]
        self.keys = keys

    def open(self):
        self.events.append("open")
        if self.fail == "open":
            raise RuntimeError("secret key do not display")
        return self.session

    def get_license_challenge(self, session, initialization):
        self.events.append("challenge")
        self.challenge_input = initialization
        if self.fail == "challenge":
            raise RuntimeError("secret key do not display")
        return (
            "<soap>challenge</soap>"
            if self.backend == DrmBackend.PLAYREADY
            else b"challenge"
        )

    def parse_license(self, session, license_data):
        self.events.append("parse")
        self.license_data = license_data
        if self.fail == "parse":
            raise RuntimeError("secret key do not display")

    def get_keys(self, session):
        self.events.append("keys")
        return self.keys

    def close(self, session):
        self.events.append("close")
        if self.fail == "close":
            raise RuntimeError("secret key do not display")


def make_song(*, payload=None, is_library=False):
    response = payload or playlist()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=response))
    )
    api = Mock()
    api.get_license_exchange = AsyncMock(
        return_value={
            "status": 0,
            "license": base64.b64encode(b"example license").decode(),
        }
    )
    song = Song.from_id(
        "i.111" if is_library else "111", api=api, is_library=is_library
    )
    stream = WebPlaybackStream(
        codec=SongCodec.AAC_WEB,
        origin=StreamOrigin.WEBPLAYBACK,
        url=URL,
        media_id=song.id,
        transport=StreamTransport(client),
    )
    return song, stream, client


class MediaDrmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Actual package parsers are independently exercised below when their
        # optional extras are present. License orchestration is always tested.
        self.widevine_patch = patch(
            "gamdl.media.drm.service._widevine_pssh", return_value=PSSH()
        )
        self.playready_patch = patch(
            "gamdl.media.drm.service._playready_header", return_value=WRMHeader()
        )
        self.widevine_patch.start()
        self.playready_patch.start()
        self.addAsyncCleanup(self.playready_patch.stop)
        self.addAsyncCleanup(self.widevine_patch.stop)

    async def test_widevine_acquires_content_only_closes_and_does_not_cache(self):
        song, stream, client = make_song()
        cdm = FakeCdm()
        async with client:
            for _ in range(2):
                keys = await song.get_keys(stream, cdm=cdm)
                self.assertEqual(keys, (ContentKey(KID, SECRET),))
            self.assertEqual(song.api.get_license_exchange.await_count, 2)
            self.assertEqual(cdm.events.count("close"), 2)
            self.assertEqual(cdm.events.count("open"), 2)
            self.assertEqual(repr(keys[0]), "ContentKey(<redacted>)")
            self.assertNotIn(SECRET.hex(), repr(keys))
            self.assertEqual(song._cache, {})
            song.api.get_license_exchange.assert_awaited_with(
                "111",
                KEY_URI,
                base64.b64encode(b"challenge").decode(),
                key_system="com.widevine.alpha",
                is_library=False,
            )
            self.assertEqual(
                cdm.license_data, base64.b64encode(b"example license").decode()
            )
            self.assertEqual(type(cdm.challenge_input).__name__, "PSSH")

    async def test_playready_uses_xml_challenge_and_decoded_license(self):
        song, stream, client = make_song()
        cdm = FakeCdm(backend=DrmBackend.PLAYREADY)
        cdm.keys.insert(
            0,
            Mock(
                key_id=UUID(bytes=KID),
                key=b"\x77" * 16,
                key_type=SimpleNamespace(name="KEYEXCHANGE"),
            ),
        )
        async with client:
            keys = await song.get_keys(stream, backend=DrmBackend.PLAYREADY, cdm=cdm)
            self.assertEqual(keys[0].kid, KID)
            self.assertEqual(keys[0].key, SECRET)
            self.assertEqual(type(cdm.challenge_input).__name__, "WRMHeader")
            self.assertEqual(cdm.license_data, "example license")
            self.assertEqual(cdm.events[-1], "close")
            song.api.get_license_exchange.assert_awaited_once_with(
                "111",
                KEY_URI,
                base64.b64encode(b"<soap>challenge</soap>").decode(),
                key_system="com.microsoft.playready",
                is_library=False,
            )

    async def test_explicit_keyformat_preserves_license_uri(self):
        for backend, keyformat in (
            (DrmBackend.WIDEVINE, "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"),
            (DrmBackend.PLAYREADY, "com.microsoft.playready"),
        ):
            with self.subTest(backend=backend):
                song, stream, client = make_song(
                    payload=playlist(keyformat=keyformat, method="SAMPLE-AES")
                )
                cdm = FakeCdm(backend=backend)
                async with client:
                    drm = await stream.get_drm_info()
                    self.assertTrue(drm.widevine_pssh or drm.playready_pssh)
                    self.assertEqual(
                        drm.widevine_uri
                        if backend == DrmBackend.WIDEVINE
                        else drm.playready_uri,
                        KEY_URI,
                    )
                    await song.get_keys(stream, backend=backend, cdm=cdm)

    async def test_fairplay_only_raises_without_contacting_license_endpoint(self):
        song, stream, client = make_song(
            payload=playlist(
                "com.apple.streamingkeydelivery",
                "SAMPLE-AES",
                "skd://itunes.apple.com/track",
            )
        )
        cdm = FakeCdm()
        async with client:
            with self.assertRaises(DrmMetadataError):
                await song.get_keys(stream, cdm=cdm)
            self.assertEqual(cdm.events, [])
            song.api.get_license_exchange.assert_not_awaited()

    async def test_unknown_signaling_does_not_get_treated_as_widevine(self):
        song, stream, client = make_song(payload=playlist(method="SAMPLE-AES"))
        async with client:
            with self.assertRaises(DrmMetadataError):
                await song.get_keys(stream, cdm=FakeCdm())

    async def test_drm_free_returns_empty_without_credentials_or_network(self):
        song, stream, client = make_song()
        stream.drm_free = True
        async with client:
            self.assertEqual(await song.get_keys(stream), ())
            song.api.get_license_exchange.assert_not_awaited()
            self.assertEqual(stream._cache, {})

    async def test_wrong_media_rejected(self):
        song, stream, client = make_song()
        another_song = Song.from_id("different-id", api=song.api)
        async with client:
            with self.assertRaises(GamdlMediaError):
                await another_song.get_keys(stream)
            self.assertEqual(stream._cache, {})

    async def test_missing_or_duplicate_cdm_parameters(self):
        song, stream, client = make_song()
        async with client:
            with self.assertRaises(DrmConfigurationError):
                await song.get_keys(stream)
            with self.assertRaises(DrmConfigurationError):
                await song.get_keys(stream, device_path="private.wvd", cdm=FakeCdm())

    async def test_apple_license_rejected_closes_session_without_secret_leak(self):
        song, stream, client = make_song()
        song.api.get_license_exchange.side_effect = RuntimeError(
            "credential ABC; secret key material"
        )
        cdm = FakeCdm()
        async with client:
            with self.assertRaises(DrmLicenseError) as error:
                await song.get_keys(stream, cdm=cdm)
            self.assertEqual(cdm.events[-1], "close")
            self.assertNotIn("credential", str(error.exception))
            self.assertNotIn("secret", str(error.exception))

    async def test_cdm_failure_and_no_keys_close_session(self):
        song, stream, client = make_song()
        async with client:
            for cdm in (
                FakeCdm(fail="challenge"),
                FakeCdm(fail="parse"),
                FakeCdm(keys=[]),
            ):
                with self.subTest(fail=cdm.fail):
                    with self.assertRaises(DrmLicenseError):
                        await song.get_keys(stream, cdm=cdm)
                    self.assertEqual(cdm.events[-1], "close")

    async def test_configuration_path_never_echoed_in_failure(self):
        song, stream, client = make_song()
        async with client:
            with self.assertRaises(DrmConfigurationError) as error:
                await song.get_keys(
                    stream, device_path="/missing/sensitive-path/private-key-file.wvd"
                )
            self.assertNotIn("sensitive", str(error.exception))

    async def test_can_supply_custom_device_path_without_storing_it(self):
        song, stream, client = make_song()
        cdm = FakeCdm()
        with patch(
            "gamdl.media.drm.service._make_cdm",
            new_callable=AsyncMock,
            return_value=cdm,
        ) as factory:
            async with client:
                await song.get_keys(stream, device_path="example.wvd")
        factory.assert_awaited_once_with(DrmBackend.WIDEVINE, "example.wvd")
        self.assertNotIn("device_path", song.__dict__)


class DrmInitializationTests(unittest.TestCase):
    @unittest.skipUnless(
        importlib.util.find_spec("pywidevine"), "pywidevine is optional"
    )
    def test_reconstruct_widevine_single_kid(self):
        pssh = _widevine_pssh(KID)
        self.assertEqual(pssh.key_ids, [UUID(bytes=KID)])

    @unittest.skipUnless(
        importlib.util.find_spec("pyplayready"), "pyplayready is optional"
    )
    def test_reconstruct_playready_single_kid(self):
        header = _playready_header(KID)
        self.assertIn("WRMHEADER", header._root.tag)

    def test_unsupported_malformed_pssh_is_sanitized(self):
        if importlib.util.find_spec("pywidevine"):
            with self.assertRaises(DrmMetadataError):
                _widevine_pssh(b"broken invalid data")
        if importlib.util.find_spec("pyplayready"):
            with self.assertRaises(DrmMetadataError):
                _playready_header(b"broken invalid data")


if __name__ == "__main__":
    unittest.main()
