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
    StreamSourceOption,
    StreamSourceSelectionRequest,
    StreamSelectionOption,
    StreamSelectionRequest,
)
from .stream import (
    DRMInfo,
    AudioStream,
    HlsSongStream,
    HlsType,
    SongCodec,
    StreamOrigin,
    StreamSegment,
    StreamSource,
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
    "HlsType",
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
    "StreamSource",
    "StreamSourceOption",
    "StreamSourceSelectionRequest",
    "StreamSelectionOption",
    "StreamSelectionRequest",
    "Tags",
    "UnsupportedMediaError",
    "UploadedVideo",
    "WebPlaybackStream",
]
