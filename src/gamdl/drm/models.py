"""DRM signaling and sensitive key descriptors."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, repr=False)
class ContentKey:
    """A license's content key and 16-byte identifier (both kept in memory).

    The default repr deliberately never prints sensitive key material. Key
    bytes are returned for callers that need them in their media pipeline.
    """

    kid: bytes
    key: bytes

    def __repr__(self) -> str:
        return "ContentKey(<redacted>)"


@dataclass(frozen=True)
class DRMInfo:
    """DRM signaling only; no secret keys or license responses.

    PSSH fields hold decoded data-URI bytes when available, not keys.
    URI fields preserve the original HLS reference for license exchanges,
    regardless of whether its data bytes could also be decoded.
    """

    widevine_pssh: bytes | None = None
    playready_pssh: bytes | None = None
    fairplay_streaming: str | None = None
    widevine_uri: str | None = None
    playready_uri: str | None = None
    untyped_key_data: bytes | None = None
    untyped_key_uri: str | None = None
    encryption_method: str | None = None

    @property
    def is_protected(self) -> bool:
        return self.encryption_method not in {None, "NONE"} or any(
            value is not None
            for value in (
                self.widevine_pssh,
                self.playready_pssh,
                self.fairplay_streaming,
                self.widevine_uri,
                self.playready_uri,
                self.untyped_key_data,
                self.untyped_key_uri,
            )
        )
