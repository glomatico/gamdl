"""Shared immutable descriptors for media, HLS, and selection requests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .enums import SelectionKind

if TYPE_CHECKING:
    from .base import Media


@dataclass(frozen=True)
class MediaSource:
    """Context for one occurrence within an album, playlist or other source."""

    kind: str
    id: str
    index: int


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


@dataclass(frozen=True)
class StreamSegment:
    url: str
    duration: float | None = None
    byte_range: str | None = None


@dataclass(frozen=True)
class SelectionOption:
    """A selectable item, identified by its occurrence index (not media ID)."""

    index: int
    title: str
    media: Media


@dataclass(frozen=True)
class SelectionRequest:
    """A UI-independent request for a choice among discovered media."""

    kind: SelectionKind
    source_id: str
    options: tuple[SelectionOption, ...]
