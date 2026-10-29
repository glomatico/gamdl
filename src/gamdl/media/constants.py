"""Defaults shared by media source-resolution policies."""

from .enums import SelectionKind

DEFAULT_AUTO_SELECT = frozenset(
    {SelectionKind.ALBUM_TRACKS, SelectionKind.PLAYLIST_TRACKS}
)

LIBRARY_COLLECTIONS: dict[SelectionKind, tuple[str, str]] = {
    SelectionKind.LIBRARY_PLAYLISTS: ("playlists", "Playlists"),
    SelectionKind.LIBRARY_ALBUMS: ("albums", "Albums"),
    SelectionKind.LIBRARY_SONGS: ("songs", "Songs"),
    SelectionKind.LIBRARY_MUSIC_VIDEOS: ("music_videos", "Music Videos"),
}

ARTIST_COLLECTIONS: dict[SelectionKind, tuple[str, str, str]] = {
    SelectionKind.ARTIST_MAIN_ALBUMS: ("views", "full-albums", "Main Albums"),
    SelectionKind.ARTIST_COMPILATION_ALBUMS: (
        "views",
        "compilation-albums",
        "Compilation Albums",
    ),
    SelectionKind.ARTIST_LIVE_ALBUMS: ("views", "live-albums", "Live Albums"),
    SelectionKind.ARTIST_SINGLES_EPS: ("views", "singles", "Singles & EPs"),
    SelectionKind.ARTIST_ALL_ALBUMS: ("relationships", "albums", "All Albums"),
    SelectionKind.ARTIST_TOP_SONGS: ("views", "top-songs", "Top Songs"),
    SelectionKind.ARTIST_MUSIC_VIDEOS: (
        "relationships",
        "music-videos",
        "Music Videos",
    ),
}

# CLI/YAML names are intentionally independent of the internal source kinds.
ARTIST_AUTO_SELECT_NAMES: dict[str, SelectionKind] = {
    kind.value.removeprefix("artist-"): kind for kind in ARTIST_COLLECTIONS
}
LIBRARY_AUTO_SELECT_NAMES: dict[str, SelectionKind] = {
    "playlists": SelectionKind.LIBRARY_PLAYLISTS,
    "albums": SelectionKind.LIBRARY_ALBUMS,
    "songs": SelectionKind.LIBRARY_SONGS,
    "music-videos": SelectionKind.LIBRARY_MUSIC_VIDEOS,
}
RIP_AUTO_SELECT_NAMES = ARTIST_AUTO_SELECT_NAMES  # Legacy internal import.
DEFAULT_RIP_AUTO_SELECT: tuple[str, ...] = ()
