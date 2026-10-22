"""DRM backend factory and license exchange contracts; no real licenses."""

import base64
import importlib.util
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import httpx

from gamdl.drm import (
    ContentKey,
    DRMInfo,
    Drm,
    DrmBackend,
    DrmConfigurationError,
    DrmLicenseError,
    DrmMetadataError,
)
from gamdl.drm.playready import _playready_header
from gamdl.drm.widevine import _widevine_pssh
from gamdl.media import SongCodec, StreamOrigin, WebPlaybackStream
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


def make_stream(*, payload=None):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text=payload or playlist())
        )
    )
    stream = WebPlaybackStream(
        codec=SongCodec.AAC_WEB,
        origin=StreamOrigin.WEBPLAYBACK,
        url=URL,
        transport=StreamTransport(client),
    )
    return stream, client


def fake_api():
    api = Mock()
    api.get_license_exchange = AsyncMock(
        return_value={
            "status": 0,
            "license": base64.b64encode(b"example license").decode(),
        }
    )
    return api


def new_handler(api, backend=DrmBackend.WIDEVINE, *, cdm=None, device_path=None):
    factory = (
        Drm.from_widevine if backend == DrmBackend.WIDEVINE else Drm.from_playready
    )
    return factory(api, cdm=cdm, device_path=device_path)


class DrmHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.widevine_patch = patch(
            "gamdl.drm.service._widevine_pssh", return_value=PSSH()
        )
        self.playready_patch = patch(
            "gamdl.drm.service._playready_header", return_value=WRMHeader()
        )
        self.widevine_patch.start()
        self.playready_patch.start()
        self.addAsyncCleanup(self.playready_patch.stop)
        self.addAsyncCleanup(self.widevine_patch.stop)

    async def test_widevine_content_only_sessions_and_no_key_caching(self):
        stream, client = make_stream()
        api, cdm = fake_api(), FakeCdm()
        drm = Drm.from_widevine(api, cdm=cdm)
        async with client:
            info = await stream.get_drm_info()
            for _ in range(2):
                keys = await drm.get_keys(media_id="111", drm_info=info)
                self.assertEqual(keys, (ContentKey(KID, SECRET),))
            self.assertEqual(api.get_license_exchange.await_count, 2)
            self.assertEqual(cdm.events.count("close"), 2)
            self.assertEqual(cdm.events.count("open"), 2)
            self.assertEqual(repr(keys[0]), "ContentKey(<redacted>)")
            self.assertNotIn(SECRET.hex(), repr(keys))
            api.get_license_exchange.assert_awaited_with(
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

    async def test_playready_xml_and_library_metadata(self):
        stream, client = make_stream()
        api, cdm = fake_api(), FakeCdm(backend=DrmBackend.PLAYREADY)
        cdm.keys.insert(
            0,
            Mock(
                key_id=UUID(bytes=KID),
                key=b"\x77" * 16,
                key_type=SimpleNamespace(name="KEYEXCHANGE"),
            ),
        )
        drm = Drm.from_playready(api, cdm=cdm)
        async with client:
            keys = await drm.get_keys(
                media_id="i.111",
                drm_info=await stream.get_drm_info(),
                is_library=True,
            )
        self.assertEqual(keys[0].kid, KID)
        self.assertEqual(keys[0].key, SECRET)
        self.assertEqual(type(cdm.challenge_input).__name__, "WRMHeader")
        self.assertEqual(cdm.license_data, "example license")
        self.assertEqual(cdm.events[-1], "close")
        api.get_license_exchange.assert_awaited_once_with(
            "i.111",
            KEY_URI,
            base64.b64encode(b"<soap>challenge</soap>").decode(),
            key_system="com.microsoft.playready",
            is_library=True,
        )

    async def test_explicit_signaling_uri_and_backend_selection(self):
        for backend, keyformat in (
            (DrmBackend.WIDEVINE, "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"),
            (DrmBackend.PLAYREADY, "com.microsoft.playready"),
        ):
            with self.subTest(backend=backend):
                stream, client = make_stream(
                    payload=playlist(keyformat=keyformat, method="SAMPLE-AES")
                )
                api = fake_api()
                drm = new_handler(api, backend, cdm=FakeCdm(backend=backend))
                async with client:
                    info = await stream.get_drm_info()
                    self.assertEqual(
                        info.widevine_uri
                        if backend == DrmBackend.WIDEVINE
                        else info.playready_uri,
                        KEY_URI,
                    )
                    await drm.get_keys(media_id="111", drm_info=info)

    async def test_backend_chooses_only_its_own_signaling(self):
        info = DRMInfo(
            widevine_pssh=KID,
            widevine_uri="data:wv",
            playready_pssh=b"p" * 16,
            playready_uri="data:pr",
            encryption_method="SAMPLE-AES",
        )
        for backend, expected_uri in (
            (DrmBackend.WIDEVINE, "data:wv"),
            (DrmBackend.PLAYREADY, "data:pr"),
        ):
            api = fake_api()
            drm = new_handler(api, backend, cdm=FakeCdm(backend=backend))
            await drm.get_keys(media_id="111", drm_info=info)
            self.assertEqual(api.get_license_exchange.await_args.args[1], expected_uri)

    async def test_fairplay_only_raises_before_cdm_or_license_request(self):
        info = DRMInfo(
            fairplay_streaming="skd://itunes.apple.com/track",
            encryption_method="SAMPLE-AES",
        )
        for backend in DrmBackend:
            api, cdm = fake_api(), FakeCdm(backend=backend)
            drm = new_handler(api, backend, cdm=cdm)
            with self.assertRaises(DrmMetadataError):
                await drm.get_keys(media_id="111", drm_info=info)
            self.assertEqual(cdm.events, [])
            api.get_license_exchange.assert_not_awaited()

    async def test_unknown_untyped_signaling_rejected(self):
        info = DRMInfo(
            untyped_key_data=KID,
            untyped_key_uri=KEY_URI,
            encryption_method="SAMPLE-AES",
        )
        drm = Drm.from_widevine(fake_api(), cdm=FakeCdm())
        with self.assertRaises(DrmMetadataError):
            await drm.get_keys(media_id="111", drm_info=info)

    async def test_unprotected_info_returns_empty_without_initializing_cdm(self):
        api = fake_api()
        drm = Drm.from_widevine(api)
        self.assertEqual(await drm.get_keys(media_id="111", drm_info=DRMInfo()), ())
        self.assertIsNone(drm._cdm)
        api.get_license_exchange.assert_not_awaited()

    async def test_invalid_input_rejected(self):
        drm = Drm.from_widevine(fake_api(), cdm=FakeCdm())
        with self.assertRaises(ValueError):
            await drm.get_keys(media_id="", drm_info=DRMInfo())
        with self.assertRaises(TypeError):
            await drm.get_keys(media_id="1", drm_info=None)

    async def test_factory_validation_and_default_path(self):
        api = fake_api()
        self.assertEqual(Drm.from_widevine(api).backend, DrmBackend.WIDEVINE)
        with self.assertRaises(DrmConfigurationError):
            Drm.from_playready(api)
        for factory in (Drm.from_playready, Drm.from_widevine):
            with self.assertRaises(DrmConfigurationError):
                factory(api, cdm=FakeCdm(), device_path="device")
        self.assertEqual(
            Drm.from_playready(api, device_path="device.prd").backend,
            DrmBackend.PLAYREADY,
        )

    async def test_license_failure_closes_session_and_redacts_error(self):
        api = fake_api()
        api.get_license_exchange.side_effect = RuntimeError(
            "credential ABC; secret key material"
        )
        cdm = FakeCdm()
        drm = Drm.from_widevine(api, cdm=cdm)
        with self.assertRaises(DrmLicenseError) as error:
            await drm.get_keys(
                media_id="111",
                drm_info=DRMInfo(
                    untyped_key_data=KID,
                    untyped_key_uri=KEY_URI,
                    encryption_method="ISO-23001-7",
                ),
            )
        self.assertEqual(cdm.events[-1], "close")
        self.assertNotIn("credential", str(error.exception))
        self.assertNotIn("secret", str(error.exception))

    async def test_cdm_errors_and_no_keys_close_sessions(self):
        for cdm in (
            FakeCdm(fail="challenge"),
            FakeCdm(fail="parse"),
            FakeCdm(keys=[]),
        ):
            with self.subTest(fail=cdm.fail):
                drm = Drm.from_widevine(fake_api(), cdm=cdm)
                with self.assertRaises(DrmLicenseError):
                    await drm.get_keys(
                        media_id="111",
                        drm_info=DRMInfo(
                            untyped_key_data=KID,
                            untyped_key_uri=KEY_URI,
                            encryption_method="ISO-23001-7",
                        ),
                    )
                self.assertEqual(cdm.events[-1], "close")

    async def test_custom_device_lazy_initialized_only_once(self):
        api = fake_api()
        cdm = FakeCdm()
        with patch(
            "gamdl.drm.service._make_cdm",
            new_callable=AsyncMock,
            return_value=cdm,
        ) as factory:
            drm = Drm.from_widevine(api, device_path="example.wvd")
            self.assertIsNone(drm._cdm)
            info = DRMInfo(
                untyped_key_data=KID,
                untyped_key_uri=KEY_URI,
                encryption_method="ISO-23001-7",
            )
            await drm.get_keys(media_id="1", drm_info=info)
            await drm.get_keys(media_id="1", drm_info=info)
            factory.assert_awaited_once_with(DrmBackend.WIDEVINE, "example.wvd")

    async def test_concurrent_requests_initialize_one_shared_cdm(self):
        api = fake_api()
        cdm = FakeCdm()
        info = DRMInfo(
            untyped_key_data=KID,
            untyped_key_uri=KEY_URI,
            encryption_method="ISO-23001-7",
        )
        with patch(
            "gamdl.drm.service._make_cdm",
            new_callable=AsyncMock,
            return_value=cdm,
        ) as constructor:
            drm = Drm.from_widevine(api)
            keys = await asyncio.gather(
                drm.get_keys(media_id="111", drm_info=info),
                drm.get_keys(media_id="222", drm_info=info),
            )
        self.assertEqual(keys, [(ContentKey(KID, SECRET),)] * 2)
        constructor.assert_awaited_once_with(DrmBackend.WIDEVINE, None)
        self.assertEqual(api.get_license_exchange.await_count, 2)
        self.assertEqual(cdm.events.count("close"), 2)

    async def test_multiple_content_keys_preserved(self):
        second_kid = bytes.fromhex("ffeeddccbbaa99887766554433221100")
        second_key = b"2" * 16
        cdm = FakeCdm(
            keys=[
                Mock(type="CONTENT", kid=UUID(bytes=KID), key=SECRET),
                Mock(type="CONTENT", kid=UUID(bytes=second_kid), key=second_key),
            ]
        )
        drm = Drm.from_widevine(fake_api(), cdm=cdm)
        keys = await drm.get_keys(
            media_id="111",
            drm_info=DRMInfo(
                widevine_pssh=KID,
                widevine_uri=KEY_URI,
                encryption_method="SAMPLE-AES",
            ),
        )
        self.assertEqual(
            keys, (ContentKey(KID, SECRET), ContentKey(second_kid, second_key))
        )

    async def test_invalid_device_file_does_not_leak_path(self):
        drm = Drm.from_widevine(
            fake_api(), device_path="/missing/sensitive-path/private-key-file.wvd"
        )
        with self.assertRaises(DrmConfigurationError) as error:
            await drm.get_keys(
                media_id="111",
                drm_info=DRMInfo(
                    untyped_key_data=KID,
                    untyped_key_uri=KEY_URI,
                    encryption_method="ISO-23001-7",
                ),
            )
        self.assertNotIn("sensitive", str(error.exception))


class DrmInitializationTests(unittest.TestCase):
    @unittest.skipUnless(
        importlib.util.find_spec("pywidevine"), "pywidevine is optional"
    )
    def test_bundled_default_widevine_device_initializes_without_path(self):
        # Only creates a CDM. Does not open a license session or perform I/O.
        from gamdl.drm.service import _make_cdm

        cdm = asyncio.run(_make_cdm(DrmBackend.WIDEVINE, None))
        self.assertEqual(type(cdm).__name__, "Cdm")

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

    def test_malformed_pssh_is_sanitized(self):
        if importlib.util.find_spec("pywidevine"):
            with self.assertRaises(DrmMetadataError):
                _widevine_pssh(b"broken invalid data")
        if importlib.util.find_spec("pyplayready"):
            with self.assertRaises(DrmMetadataError):
                _playready_header(b"broken invalid data")


if __name__ == "__main__":
    unittest.main()
