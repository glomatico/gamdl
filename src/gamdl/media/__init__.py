"""Lazy Apple Music media models, source resolution, and data transformations."""

from gamdl.pagination import MediaPage, PageCache

from .base import Media, MediaSource
from .cover import Cover
from .cover_cache import CoverCache
from .enums import CoverFormat, HlsType, StreamOrigin, StreamSource
from .exceptions import (
    GamdlMediaError,
    MediaNotFoundError,
    MediaNotStreamableError,
    SelectionRequiredError,
    StreamManifestError,
    StreamNotAvailableError,
    UnsupportedMediaError,
)
from .models import (
    ArtistBrowseRequest,
    ArtistBrowseResult,
    ArtistCategorySelection,
    CollectionOption,
    CollectionSelectionRequest,
)
from .music_video import (
    AudioSelectionOption,
    AudioSelectionRequest,
    MusicVideo,
    MusicVideoCodec,
    MusicVideoStreamSelector,
    VideoSelectionOption,
    VideoSelectionRequest,
)
from .music_video.stream import MusicVideoStreams
from .playlist import Playlist, PlaylistMetadata
from .resolver import (
    DEFAULT_AUTO_SELECT,
    MediaResolver,
    SelectionKind,
    SelectionOption,
    SelectionRequest,
)
from .song import Song, SongStreamSelector
from .song.enums import SongCodec
from .song.lyrics import Lyrics, LyricsLine, LyricsSegment
from .song.models import (
    StreamSelectionOption,
    StreamSelectionRequest,
    StreamSourceOption,
    StreamSourceSelectionRequest,
)
from .song.stream import SongStreamResolver
from .stream import (
    AudioStream,
    HlsSongStream,
    HlsStream,
    HttpStream,
    StreamSegment,
    VideoStream,
    WebPlaybackStream,
)
from .uploaded_video import (
    UploadedVideo,
    UploadedVideoQuality,
    UploadedVideoSelectionOption,
    UploadedVideoSelectionRequest,
    UploadedVideoStreamSelector,
)
from .url import AppleMusicUrlInfo

__all__ = [
    "AppleMusicUrlInfo",
    "MediaPage",
    "PageCache",
    "ArtistBrowseRequest",
    "ArtistBrowseResult",
    "ArtistCategorySelection",
    "CollectionOption",
    "CollectionSelectionRequest",
    "Playlist",
    "PlaylistMetadata",
    "AudioStream",
    "HlsStream",
    "Cover",
    "CoverCache",
    "CoverFormat",
    "DEFAULT_AUTO_SELECT",
    "GamdlMediaError",
    "MediaNotFoundError",
    "MediaNotStreamableError",
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
