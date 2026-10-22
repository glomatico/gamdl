"""Compatibility imports for the dedicated video media packages."""

from .base import _first_resource
from .music_video import MusicVideo
from .uploaded_video import UploadedVideo

__all__ = ["MusicVideo", "UploadedVideo"]
