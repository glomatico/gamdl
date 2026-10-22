"""Compatibility imports for the song-specific stream resolver.

New code may import from :mod:`gamdl.media.song.stream` instead.
"""

from .song.models import (
    StreamSelector,
    StreamSelectionOption,
    StreamSelectionRequest,
    StreamSourceOption,
    StreamSourceSelectionRequest,
)
from .song.stream import (
    SongStreamResolver,
    _default_master_url,
    _first_song,
    match_hls_codec,
)

__all__ = [
    "SongStreamResolver",
    "StreamSelector",
    "StreamSelectionOption",
    "StreamSelectionRequest",
    "StreamSourceOption",
    "StreamSourceSelectionRequest",
    "match_hls_codec",
]
