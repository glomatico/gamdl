"""Shared immutable descriptors for media, HLS, and selection requests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .enums import SelectionKind

if TYPE_CHECKING:
    from .base import Media
    from .playlist import Playlist


@dataclass(frozen=True)
class MediaSource:
    """Context for one occurrence within an album, playlist or other source."""

    kind: str
    id: str
    index: int
    playlist: Playlist | None = None


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
