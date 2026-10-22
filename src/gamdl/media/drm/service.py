"""Backend-independent authorized DRM license orchestration."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ..enums import DrmBackend
from ..exceptions import DrmConfigurationError, DrmLicenseError, DrmMetadataError
from .models import ContentKey
from .playready import _playready_header
from .widevine import _widevine_pssh

if TYPE_CHECKING:
    from gamdl.api import AppleMusicApi
    from ..models import DRMInfo
    from ..stream import AudioStream


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


async def _make_cdm(backend: DrmBackend, device_path: str | Path) -> Any:
    """Create an independent CDM from caller-owned, locally provisioned data."""
    try:
        if backend == DrmBackend.WIDEVINE:
            from pywidevine import Cdm, Device
        else:
            from pyplayready.cdm import Cdm
            from pyplayready.device import Device
    except ImportError:
        extra = backend.value
        raise DrmConfigurationError(
            f"{extra} backend not installed; install gamdl[{extra}]"
        ) from None

    def create() -> Any:
        return Cdm.from_device(Device.load(str(device_path)))

    try:
        return await asyncio.to_thread(create)
    except Exception:
        # Device paths and embedded private material must not reach exceptions.
        raise DrmConfigurationError(
            "Unable to load the provisioned DRM device"
        ) from None


def _normalize_keys(backend: DrmBackend, keys: Any) -> tuple[ContentKey, ...]:
    result: list[ContentKey] = []
    seen: set[bytes] = set()
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
        if kid_bytes not in seen:
            result.append(ContentKey(kid_bytes, key_bytes))
            seen.add(kid_bytes)
    if not result:
        raise DrmLicenseError("DRM license contained no usable content keys")
    return tuple(result)


async def get_stream_keys(
    api: AppleMusicApi,
    *,
    stream: AudioStream,
    media_id: str,
    is_library: bool,
    backend: DrmBackend,
    device_path: str | Path | None,
    cdm: Any | None,
) -> tuple[ContentKey, ...]:
    """Run one explicit, authorized license exchange and return content keys.

    Sessions close even on failures/cancellation. License responses and keys
    never enter the media/stream caches. The CDM and API remain caller-owned.
    """
    if stream.drm_free:
        return ()
    if (device_path is None) == (cdm is None):
        raise DrmConfigurationError("Provide exactly one of device_path or cdm")

    drm = await stream.get_drm_info()
    if not drm.is_protected:
        return ()
    data, uri = _get_signaling(drm, backend)
    initialization = (
        _widevine_pssh(data)
        if backend == DrmBackend.WIDEVINE
        else _playready_header(data)
    )
    module = cdm if cdm is not None else await _make_cdm(backend, device_path)
    try:
        session = await asyncio.to_thread(module.open)
    except Exception:
        raise DrmLicenseError("Unable to open DRM session") from None

    try:
        try:
            challenge = await asyncio.to_thread(
                module.get_license_challenge, session, initialization
            )
            if backend == DrmBackend.PLAYREADY:
                if not isinstance(challenge, str):
                    raise ValueError("Expected XML PlayReady challenge")
                raw_challenge = challenge.encode("utf-8")
            else:
                raw_challenge = bytes(challenge)
            response = await api.get_license_exchange(
                media_id,
                uri,
                base64.b64encode(raw_challenge).decode("ascii"),
                key_system=backend.key_system,
                is_library=is_library,
            )
            license_data = response.get("license")
            if not isinstance(license_data, str) or not license_data:
                raise ValueError("Missing license response")
            if backend == DrmBackend.PLAYREADY:
                decoded_license = base64.b64decode(license_data, validate=True).decode(
                    "utf-8-sig"
                )
            else:
                decoded_license = license_data
            await asyncio.to_thread(module.parse_license, session, decoded_license)
            keys = await asyncio.to_thread(module.get_keys, session)
            return _normalize_keys(backend, keys)
        except DrmLicenseError:
            raise
        except Exception:
            # The original exception may contain license bodies, secret keys,
            # credential paths or Apple-signed playback URIs.
            raise DrmLicenseError("Unable to obtain or process DRM license") from None
    finally:
        try:
            await asyncio.to_thread(module.close, session)
        except Exception:
            raise DrmLicenseError("Unable to close DRM session") from None
