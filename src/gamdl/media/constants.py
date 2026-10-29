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
RIP_AUTO_SELECT_NAMES: dict[str, SelectionKind] = {
    "albums": SelectionKind.ARTIST_ALL_ALBUMS,
    "music-videos": SelectionKind.ARTIST_MUSIC_VIDEOS,
    **{kind.value: kind for kind in ARTIST_COLLECTIONS},
}
DEFAULT_RIP_AUTO_SELECT: tuple[str, ...] = ()
