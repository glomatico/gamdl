"""Lazy Apple Music media models, source resolution, and data transformations."""

from .base import Media, MediaSource
from .cover import Cover
from .exceptions import (
    GamdlMediaError,
    SelectionRequiredError,
    StreamManifestError,
    StreamNotAvailableError,
    UnsupportedMediaError,
)
from .lyrics import Lyrics, LyricsLine, LyricsSegment
from .resolver import (
    DEFAULT_AUTO_SELECT,
    MediaResolver,
    SelectionKind,
    SelectionOption,
    SelectionRequest,
)
from .song import Song
from .song_stream import (
    SongStreamResolver,
    StreamSelectionOption,
    StreamSelectionRequest,
)
from .stream import (
    DRMInfo,
    AudioStream,
    HlsSongStream,
    SongCodec,
    StreamOrigin,
    StreamSegment,
    WebPlaybackStream,
)
from .tags import Tags
from .video import MusicVideo, UploadedVideo

__all__ = [
    "AudioStream",
    "Cover",
    "DEFAULT_AUTO_SELECT",
    "DRMInfo",
    "GamdlMediaError",
    "HlsSongStream",
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
    "SongCodec",
    "SongStreamResolver",
    "StreamManifestError",
    "StreamNotAvailableError",
    "StreamOrigin",
    "StreamSegment",
    "StreamSelectionOption",
    "StreamSelectionRequest",
    "Tags",
    "UnsupportedMediaError",
    "UploadedVideo",
    "WebPlaybackStream",
]
