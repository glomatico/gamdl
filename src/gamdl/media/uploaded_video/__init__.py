"""Apple Music uploaded video metadata model."""

from .media import UploadedVideo
from .stream import (
    UploadedVideoQuality,
    UploadedVideoSelectionOption,
    UploadedVideoSelectionRequest,
    UploadedVideoStreamSelector,
)

__all__ = [
    "UploadedVideo",
    "UploadedVideoQuality",
    "UploadedVideoSelectionOption",
    "UploadedVideoSelectionRequest",
    "UploadedVideoStreamSelector",
]
