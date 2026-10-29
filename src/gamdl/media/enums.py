"""Media-wide enum identifiers shared by generic stream and media layers."""

from __future__ import annotations

from enum import Enum


class StreamOrigin(str, Enum):
    WEBPLAYBACK = "webplayback"
    LIBRARY_WEBPLAYBACK = "library-webplayback"
    WRAPPER_HLS = "wrapper-hls"
    ASSET_ENHANCED_HLS = "asset-enhanced-hls"
    ASSET_STANDARD_HLS = "asset-standard-hls"
    ITUNES_HLS = "itunes-hls"
    UPLOADED_VIDEO = "uploaded-video"


class StreamSource(str, Enum):
    """Which service supplied the stream's playback URL."""

    WEBPLAYBACK = "webplayback"
    WRAPPER = "wrapper"
    ASSETS = "assets"
    ITUNES = "itunes"


class HlsType(str, Enum):
    """Actual master-playlist structure, not the requested HLS profile."""

    ENHANCED = "enhanced"
    STANDARD = "standard"


class CoverFormat(str, Enum):
    """Artwork formats supported by Apple Music's resizing endpoint."""

    JPG = "jpg"
    PNG = "png"


class SelectionKind(str, Enum):
    """Independent selection decisions made by the source resolver."""

    ALBUM_TRACKS = "album_tracks"
    PLAYLIST_TRACKS = "playlist_tracks"
    ARTIST = "artist"
    LIBRARY = "library"
    LIBRARY_PLAYLISTS = "library-playlists"
    LIBRARY_ALBUMS = "library-albums"
    LIBRARY_SONGS = "library-songs"
    LIBRARY_MUSIC_VIDEOS = "library-music-videos"
    ARTIST_MAIN_ALBUMS = "artist-main-albums"
    ARTIST_COMPILATION_ALBUMS = "artist-compilation-albums"
    ARTIST_LIVE_ALBUMS = "artist-live-albums"
    ARTIST_SINGLES_EPS = "artist-singles-eps"
    ARTIST_ALL_ALBUMS = "artist-all-albums"
    ARTIST_TOP_SONGS = "artist-top-songs"
    ARTIST_MUSIC_VIDEOS = "artist-music-videos"
