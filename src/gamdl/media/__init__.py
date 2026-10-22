"""Lazy Apple Music media models, source resolution, and data transformations."""

from .base import Media, MediaSource
from .cover import Cover
from .cover_cache import CoverCache
from .enums import HlsType, StreamOrigin, StreamSource
from .exceptions import (
    GamdlMediaError,
    SelectionRequiredError,
    StreamManifestError,
    StreamNotAvailableError,
    UnsupportedMediaError,
)
from .song.lyrics import Lyrics, LyricsLine, LyricsSegment
from .resolver import (
    DEFAULT_AUTO_SELECT,
    MediaResolver,
    SelectionKind,
    SelectionOption,
    SelectionRequest,
)
from .song import Song
from .song.enums import SongCodec
from .song.stream import SongStreamResolver
from .song.models import (
    StreamSourceOption,
    StreamSourceSelectionRequest,
    StreamSelectionOption,
    StreamSelectionRequest,
)
from .stream import (
    AudioStream,
    HlsSongStream,
    StreamSegment,
    WebPlaybackStream,
)
from .music_video import MusicVideo
from .uploaded_video import UploadedVideo

__all__ = [
    "AudioStream",
    "Cover",
    "CoverCache",
    "DEFAULT_AUTO_SELECT",
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
    "UnsupportedMediaError",
    "UploadedVideo",
    "WebPlaybackStream",
]
