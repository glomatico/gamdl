"""Independent Widevine and PlayReady DRM license orchestration.

The handler uses only a media ID, DRMInfo, and an injected license provider.
No dependency on Song, MediaResolver or AudioStream.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from .enums import DrmBackend
from .exceptions import DrmConfigurationError, DrmLicenseError, DrmMetadataError
from .models import ContentKey, DRMInfo
from .playready import _playready_header
from .widevine import _widevine_pssh


class LicenseProvider(Protocol):
    """The provider implements the Apple Music license exchange contract."""

    async def get_license_exchange(
        self,
        track_id: str,
        track_uri: str,
        challenge: str,
        *,
        key_system: str,
        is_library: bool,
    ) -> dict[str, Any]: ...


def _get_signaling(drm: DRMInfo, backend: DrmBackend) -> tuple[bytes, str]:
    """Resolve the correct PSSH/KID and its original Apple playback URI.

    Webplayback can supply an untyped CENC key reference. It must not be
    mistaken for Widevine- or PlayReady-specific signaling in other formats.
    """
    if backend == DrmBackend.WIDEVINE:
        data, uri = drm.widevine_pssh, drm.widevine_uri
    else:
        data, uri = drm.playready_pssh, drm.playready_uri

    if not (data and uri) and drm.encryption_method == "ISO-23001-7":
        data, uri = drm.untyped_key_data, drm.untyped_key_uri
    if not isinstance(data, bytes) or not data or not uri:
        raise DrmMetadataError("No usable DRM initialization data for selected backend")
    return data, uri


async def _make_cdm(backend: DrmBackend, device_path: str | Path | None) -> Any:
    """Construct a CDM from a provisioned path or the default Widevine device."""
    try:
        if backend == DrmBackend.WIDEVINE:
            from pywidevine import Cdm, Device

            if device_path is None:
                from .default_wvd import WVD
        else:
            from pyplayready.cdm import Cdm
            from pyplayready.device import Device
    except ImportError:
        raise DrmConfigurationError(
            "Required DRM backend or default device is not available"
        ) from None

    def create() -> Any:
        if device_path is None:
            return Cdm.from_device(Device.loads(WVD))
        return Cdm.from_device(Device.load(str(device_path)))

    try:
        return await asyncio.to_thread(create)
    except Exception:
        raise DrmConfigurationError(
            "Unable to load the provisioned DRM device"
        ) from None


def _normalize_key(backend: DrmBackend, keys: Any) -> ContentKey:
    """Return exactly one content key; never silently guess among distinct KIDs."""
    result: ContentKey | None = None
    for entry in keys:
        if backend == DrmBackend.WIDEVINE and entry.type != "CONTENT":
            continue
        if backend == DrmBackend.PLAYREADY:
            key_type = getattr(entry, "key_type", None)
            if key_type is not None and getattr(key_type, "name", None) not in {
                "AES_128_CTR",
                "AES_128_CBC",
                "AES_128_ECB",
                "RC4_CIPHER",
                "COCKTAIL",
            }:
                continue
        kid = entry.kid if backend == DrmBackend.WIDEVINE else entry.key_id
        kid_bytes = kid.bytes if isinstance(kid, UUID) else bytes(kid)
        key_bytes = bytes(entry.key)
        if len(kid_bytes) != 16 or len(key_bytes) not in {16, 24, 32}:
            raise DrmLicenseError("DRM license contained an invalid content key")
        key = ContentKey(kid_bytes, key_bytes)
        if result is None:
            result = key
        elif result != key:
            raise DrmLicenseError(
                "DRM license contained multiple distinct content keys"
            )
    if result is None:
        raise DrmLicenseError("DRM license contained no usable content keys")
    return result


class Drm:
    """A backend-specific CDM with one license provider and no media ownership.

    The CDM is initialized lazily and reused. A distinct session is created
    and closed for each license request; no key/license response is cached.
    """

    def __init__(
        self,
        license_provider: LicenseProvider,
        *,
        backend: DrmBackend,
        device_path: str | Path | None = None,
        cdm: Any | None = None,
    ) -> None:
        if cdm is not None and device_path is not None:
            raise DrmConfigurationError("Supply either a CDM or a device path")
        if backend == DrmBackend.PLAYREADY and cdm is None and device_path is None:
            raise DrmConfigurationError("PlayReady requires a device path or CDM")
        self.license_provider = license_provider
        self.backend = backend
        self._device_path = device_path
        self._cdm = cdm
        self._cdm_lock = asyncio.Lock()

    @classmethod
    def from_widevine(
        cls,
        license_provider: LicenseProvider,
        *,
        device_path: str | Path | None = None,
        cdm: Any | None = None,
    ) -> Drm:
        """Use a custom device or the default bundled Widevine device."""
        return cls(
            license_provider,
            backend=DrmBackend.WIDEVINE,
            device_path=device_path,
            cdm=cdm,
        )

    @classmethod
    def from_playready(
        cls,
        license_provider: LicenseProvider,
        *,
        device_path: str | Path | None = None,
        cdm: Any | None = None,
    ) -> Drm:
        """Require a provisioned PlayReady device or caller-owned CDM."""
        return cls(
            license_provider,
            backend=DrmBackend.PLAYREADY,
            device_path=device_path,
            cdm=cdm,
        )

    async def _get_cdm(self) -> Any:
        if self._cdm is not None:
            return self._cdm
        async with self._cdm_lock:
            if self._cdm is None:
                self._cdm = await _make_cdm(self.backend, self._device_path)
            return self._cdm

    async def get_keys(
        self, *, media_id: str, drm_info: DRMInfo, is_library: bool = False
    ) -> ContentKey | None:
        """Exchange an authorized license using a matching DRM URI and ID.

        FairPlay-only signaling cannot be used by Widevine or PlayReady.
        Unprotected resources return None without opening the CDM.
        Multiple distinct content keys are rejected rather than silently
        selecting one that may be unsuitable for the selected stream.
        """
        if not media_id:
            raise ValueError("A media ID must not be empty")
        if not isinstance(drm_info, DRMInfo):
            raise TypeError("drm_info must be a DRMInfo object")
        if not drm_info.is_protected:
            return None

        data, uri = _get_signaling(drm_info, self.backend)
        initialization = (
            _widevine_pssh(data)
            if self.backend == DrmBackend.WIDEVINE
            else _playready_header(data)
        )
        module = await self._get_cdm()
        try:
            session = await asyncio.to_thread(module.open)
        except Exception:
            raise DrmLicenseError("Unable to open DRM session") from None

        try:
            try:
                challenge = await asyncio.to_thread(
                    module.get_license_challenge, session, initialization
                )
                if self.backend == DrmBackend.PLAYREADY:
                    if not isinstance(challenge, str):
                        raise ValueError("Expected XML PlayReady challenge")
                    raw_challenge = challenge.encode("utf-8")
                else:
                    raw_challenge = bytes(challenge)
                response = await self.license_provider.get_license_exchange(
                    media_id,
                    uri,
                    base64.b64encode(raw_challenge).decode("ascii"),
                    key_system=self.backend.key_system,
                    is_library=is_library,
                )
                license_data = response.get("license")
                if not isinstance(license_data, str) or not license_data:
                    raise ValueError("Missing license response")
                if self.backend == DrmBackend.PLAYREADY:
                    decoded_license = base64.b64decode(
                        license_data, validate=True
                    ).decode("utf-8-sig")
                else:
                    decoded_license = license_data
                await asyncio.to_thread(module.parse_license, session, decoded_license)
                keys = await asyncio.to_thread(module.get_keys, session)
                return _normalize_key(self.backend, keys)
            except DrmLicenseError:
                raise
            except Exception:
                raise DrmLicenseError(
                    "Unable to obtain or process DRM license"
                ) from None
        finally:
            try:
                await asyncio.to_thread(module.close, session)
            except Exception:
                raise DrmLicenseError("Unable to close DRM session") from None
