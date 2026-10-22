"""Apple Music license-exchange endpoint; CDM handling lives in media."""

from __future__ import annotations

from typing import Any

from ..constants import APPLE_MUSIC_LICENSE_URL
from ..exceptions import GamdlApiResponseError


class DrmEndpoints:
    """DRM license exchange; requests are not automatically retried."""

    async def get_license_exchange(
        self,
        track_id: str,
        track_uri: str,
        challenge: str,
        *,
        key_system: str = "com.widevine.alpha",
        is_library: bool = False,
    ) -> dict[str, Any]:
        """Exchange a DRM challenge for the track's playback license.

        Args:
            track_id: Apple Music media identifier.
            track_uri: Stream URI associated with the license challenge.
            challenge: Encoded challenge supplied by the DRM component.
            key_system: DRM key-system identifier (defaults to Widevine).
            is_library: Whether the identifier belongs to the user's library.

        Returns:
            Raw license response from Apple Music when ``status`` is zero.

        Notes:
            License requests use POST and are **not** automatically retried.
            Neither the challenge nor license contents should appear in logs.
        """
        response = await self.http.request_json(
            "POST",
            APPLE_MUSIC_LICENSE_URL,
            operation="get_license_exchange",
            headers=self._headers,
            json={
                "challenge": challenge,
                "key-system": key_system,
                "uri": track_uri,
                "adamId": track_id,
                "isLibrary": is_library,
                "user-initiated": True,
            },
        )
        if response.get("status") != 0:
            raise GamdlApiResponseError("Apple Music license request was rejected")
        return response
