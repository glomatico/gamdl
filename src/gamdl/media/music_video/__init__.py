"""Apple Music catalog and library music videos."""

from .enums import MusicVideoCodec
from .media import MusicVideo
from .models import (
    AudioSelectionOption,
    AudioSelectionRequest,
    MusicVideoStreamSelector,
    VideoSelectionOption,
    VideoSelectionRequest,
)
from .stream import MusicVideoStreams

__all__ = [
    "MusicVideo",
    "MusicVideoCodec",
    "MusicVideoStreams",
    "MusicVideoStreamSelector",
    "VideoSelectionOption",
    "VideoSelectionRequest",
    "AudioSelectionOption",
    "AudioSelectionRequest",
]
