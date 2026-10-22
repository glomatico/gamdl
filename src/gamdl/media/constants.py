"""Defaults shared by media source-resolution policies."""

from .enums import SelectionKind

DEFAULT_AUTO_SELECT = frozenset(
    {SelectionKind.ALBUM_TRACKS, SelectionKind.PLAYLIST_TRACKS}
)
