"""Supported music-video codec identifiers."""

from __future__ import annotations

from enum import Enum


class MusicVideoCodec(str, Enum):
    """Real video codecs. Interactive selection is a separate API option."""

    H264 = "h264"
    H265 = "h265"
