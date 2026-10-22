"""Lazy Apple Music media models, source resolution, and data transformations."""

from .base import Media, MediaSource
from .cover import Cover
from .exceptions import GamdlMediaError, SelectionRequiredError, UnsupportedMediaError
from .lyrics import Lyrics, LyricsLine, LyricsSegment
from .resolver import (
    DEFAULT_AUTO_SELECT,
    MediaResolver,
    SelectionKind,
    SelectionOption,
    SelectionRequest,
)
from .song import Song
from .tags import Tags
from .video import MusicVideo, UploadedVideo

__all__ = [
    "Cover",
    "DEFAULT_AUTO_SELECT",
    "GamdlMediaError",
    "Lyrics",
    "LyricsLine",
    "LyricsSegment",
    "Media",
    "MediaResolver",
    "MediaSource",
    "MusicVideo",
    "SelectionKind",
    "SelectionOption",
    "SelectionRequest",
    "SelectionRequiredError",
    "Song",
    "Tags",
    "UnsupportedMediaError",
    "UploadedVideo",
]
