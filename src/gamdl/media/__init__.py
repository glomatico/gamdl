"""Lazy Apple Music media models, source resolution, and data transformations."""

from .base import Media, MediaSource
from .cover import Cover
from .drm import ContentKey, DrmBackend
from .enums import HlsType, StreamOrigin, StreamSource
from .exceptions import (
    GamdlMediaError,
    DrmConfigurationError,
    DrmError,
    DrmLicenseError,
    DrmMetadataError,
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
    DRMInfo,
    AudioStream,
    HlsSongStream,
    StreamSegment,
    WebPlaybackStream,
)
from .song.tags import Tags
from .music_video import MusicVideo
from .uploaded_video import UploadedVideo

__all__ = [
    "AudioStream",
    "Cover",
    "ContentKey",
    "DEFAULT_AUTO_SELECT",
    "DRMInfo",
    "GamdlMediaError",
    "DrmBackend",
    "DrmError",
    "DrmConfigurationError",
    "DrmLicenseError",
    "DrmMetadataError",
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
