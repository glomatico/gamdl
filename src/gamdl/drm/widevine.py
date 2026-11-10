"""Widevine DRM initialization-data parsing."""

from __future__ import annotations

from typing import Any

from .exceptions import DrmConfigurationError, DrmMetadataError


def _widevine_pssh(data: bytes) -> Any:
    """Accept a full PSSH/Widevine payload or a single 16-byte CENC KID."""
    try:
        from pywidevine import PSSH
        from pywidevine.license_protocol_pb2 import WidevinePsshData
    except ImportError as error:
        raise DrmConfigurationError(
            "Widevine backend not installed; install gamdl[widevine]"
        ) from error

    try:
        if len(data) == 16:
            payload = WidevinePsshData(algorithm=1, key_ids=[data]).SerializeToString()
            return PSSH(payload, strict=True)
        return PSSH(data, strict=True)
    except Exception as error:
        raise DrmMetadataError("Invalid Widevine DRM initialization data") from error
