"""Media-wide enum identifiers shared by generic stream and media layers."""

from __future__ import annotations

from enum import Enum


class StreamOrigin(str, Enum):
    WEBPLAYBACK = "webplayback"
    LIBRARY_WEBPLAYBACK = "library-webplayback"
    WRAPPER_HLS = "wrapper-hls"
    ASSET_ENHANCED_HLS = "asset-enhanced-hls"
    ASSET_STANDARD_HLS = "asset-standard-hls"


class StreamSource(str, Enum):
    """Which service supplied the stream's playback URL."""

    WEBPLAYBACK = "webplayback"
    WRAPPER = "wrapper"
    ASSETS = "assets"


class HlsType(str, Enum):
    """Actual master-playlist structure, not the requested HLS profile."""

    ENHANCED = "enhanced"
    STANDARD = "standard"


class DrmBackend(str, Enum):
    """Supported content-decryption modules for Apple Music license requests."""

    WIDEVINE = "widevine"
    PLAYREADY = "playready"

    @property
    def key_system(self) -> str:
        return {
            DrmBackend.WIDEVINE: "com.widevine.alpha",
            DrmBackend.PLAYREADY: "com.microsoft.playready",
        }[self]


class SelectionKind(str, Enum):
    """Independent selection decisions made by the source resolver."""

    ALBUM_TRACKS = "album_tracks"
    PLAYLIST_TRACKS = "playlist_tracks"
    ARTIST = "artist"
    LIBRARY = "library"
