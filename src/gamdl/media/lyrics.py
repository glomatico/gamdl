"""Compatibility imports for the song-specific lyrics models."""

from .song.lyrics import (
    Lyrics,
    LyricsLine,
    LyricsSegment,
    _local,
    _lrc_stamp,
    _milliseconds,
    _srt_stamp,
)

__all__ = ["Lyrics", "LyricsLine", "LyricsSegment"]
