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
from .song import Song, SongStreamSelector
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
    HttpStream,
    HlsStream,
    HlsSongStream,
    StreamSegment,
    VideoStream,
    WebPlaybackStream,
)
from .music_video import (
    MusicVideo,
    MusicVideoCodec,
    VideoSelectionOption,
    VideoSelectionRequest,
    AudioSelectionOption,
    AudioSelectionRequest,
    MusicVideoStreamSelector,
)
from .music_video.stream import MusicVideoStreams
from .uploaded_video import (
    UploadedVideo,
    UploadedVideoQuality,
    UploadedVideoSelectionOption,
    UploadedVideoSelectionRequest,
    UploadedVideoStreamSelector,
)

__all__ = [
    "AudioStream",
    "HlsStream",
    "Cover",
    "CoverCache",
    "DEFAULT_AUTO_SELECT",
    "GamdlMediaError",
    "HlsSongStream",
    "HttpStream",
    "HlsType",
    "Lyrics",
    "LyricsLine",
    "LyricsSegment",
    "Media",
    "MediaResolver",
    "MediaSource",
    "MusicVideo",
    "MusicVideoCodec",
    "MusicVideoStreamSelector",
    "VideoSelectionOption",
    "VideoSelectionRequest",
    "AudioSelectionOption",
    "AudioSelectionRequest",
    "MusicVideoStreams",
    "SelectionKind",
    "SelectionOption",
    "SelectionRequest",
    "SelectionRequiredError",
    "Song",
    "SongStreamSelector",
    "SongCodec",
    "SongStreamResolver",
    "StreamManifestError",
    "StreamNotAvailableError",
    "StreamOrigin",
    "StreamSegment",
    "VideoStream",
    "StreamSource",
    "StreamSourceOption",
    "StreamSourceSelectionRequest",
    "StreamSelectionOption",
    "StreamSelectionRequest",
    "UnsupportedMediaError",
    "UploadedVideo",
    "UploadedVideoQuality",
    "UploadedVideoSelectionOption",
    "UploadedVideoSelectionRequest",
    "UploadedVideoStreamSelector",
    "WebPlaybackStream",
]
