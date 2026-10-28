"""Shared immutable descriptors for media, HLS, and selection requests."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from .enums import SelectionKind
from gamdl.pagination import MediaPage

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


@dataclass(frozen=True)
class CollectionOption:
    """One category or artist item, selected by occurrence rather than ID."""

    index: int
    title: str


@dataclass(frozen=True)
class CollectionSelectionRequest:
    kind: SelectionKind
    source_id: str
    prompt: str
    options: tuple[CollectionOption, ...]


@dataclass(frozen=True)
class ArtistBrowseRequest:
    """UI-independent artist browser; category contents load on expansion."""

    source_id: str
    artist_name: str
    categories: tuple[tuple[SelectionKind, str], ...]
    load_page: Callable[
        [SelectionKind, int], Awaitable[MediaPage[CollectionOption] | None]
    ]


@dataclass(frozen=True)
class LibraryBrowseRequest(ArtistBrowseRequest):
    """Library categories use the same lazy browser and selection protocol."""


@dataclass(frozen=True)
class ArtistCategorySelection:
    """None means all items; exclusions preserve unseen page selections."""

    kind: SelectionKind
    indices: tuple[int, ...] | None = None
    excluded_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class ArtistBrowseResult:
    selections: tuple[ArtistCategorySelection, ...]
