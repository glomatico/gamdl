"""Apple Music playback assets and direct webplayback endpoints."""

from __future__ import annotations

from typing import Any

from ..constants import APPLE_MUSIC_WEBPLAYBACK_URL
from ..exceptions import GamdlApiResponseError


class PlaybackEndpoints:
    """Streaming metadata endpoints; does not fetch DRM licenses or media bytes."""

    async def get_assets(
        self,
        media_id: str,
        *,
        kind: str = "song",
        include_license_urls: bool = True,
        hls_encryption: str = "CBC",
        hls_profile: str = "enhancedHls",
    ) -> dict[str, Any]:
        """Retrieve streaming asset metadata for a song or other media item.

        ``kind`` uses Apple's asset API identifiers: ``"song"`` for songs
        and ``"musicVideo"`` for music videos (not ``"music-video"``).
        ``hls_encryption`` and ``hls_profile`` select the requested HLS
        configuration. The response is raw Apple metadata; downloading and
        decrypting media are responsibilities of higher-level components.
        """
        params: dict[str, Any] = {
            "id": media_id,
            "kind": kind,
            "includeLicenseUrls": include_license_urls,
            "hlsEncryption": hls_encryption,
        }
        if hls_profile:
            params["hlsProfile"] = hls_profile
        return await self._get("/v1/play/assets", operation="get_assets", params=params)

    async def get_webplayback(
        self, track_id: str, *, is_library: bool = False
    ) -> dict[str, Any]:
        """Request playback metadata for a catalog or library track.

        ``is_library`` sends ``universalLibraryId`` instead of the catalog
        ``salableAdamId``. The endpoint may reject unavailable media with a
        ``dialog`` response, which is converted to an API error.
        """
        body = {
            ("universalLibraryId" if is_library else "salableAdamId"): track_id,
            "language": self.language,
        }
        response = await self.http.request_json(
            "POST",
            APPLE_MUSIC_WEBPLAYBACK_URL,
            operation="get_webplayback",
            json=body,
            headers=self._headers,
        )
        if response.get("dialog"):
            raise GamdlApiResponseError("Apple Music web playback was rejected")
        return response
