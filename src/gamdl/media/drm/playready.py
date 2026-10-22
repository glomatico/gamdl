"""PlayReady DRM initialization-data and WRM header parsing."""

from __future__ import annotations

import base64
from typing import Any
from uuid import UUID

from ..exceptions import DrmConfigurationError, DrmMetadataError


def _playready_header(data: bytes) -> Any:
    """Accept a PlayReady object/header or a 16-byte CENC key identifier."""
    try:
        from pyplayready.system.pssh import PSSH
        from pyplayready.system.wrmheader import WRMHeader
    except ImportError:
        raise DrmConfigurationError(
            "PlayReady backend not installed; install gamdl[playready]"
        ) from None

    try:
        if len(data) == 16:
            key_id = base64.b64encode(UUID(bytes=data).bytes_le).decode("ascii")
            return WRMHeader(
                '<WRMHEADER xmlns="http://schemas.microsoft.com/DRM/2007/03/PlayReadyHeader" '
                'version="4.3.0.0"><DATA><PROTECTINFO><KIDS>'
                f'<KID ALGID="AESCTR" VALUE="{key_id}" />'
                "</KIDS></PROTECTINFO></DATA></WRMHEADER>"
            )
        return PSSH(data).wrm_headers[0]
    except Exception:
        raise DrmMetadataError("Invalid PlayReady DRM initialization data") from None
