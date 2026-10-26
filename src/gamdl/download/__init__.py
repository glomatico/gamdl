"""Metadata, safe publication, and single-item download orchestration."""

from .downloader import Downloader, DownloadProgress
from .models import DownloadResult, FileResult, PlaylistResult, DatabaseResult
from .enums import (
    MediaStatus,
    FileStatus,
    PlaylistStatus,
    DatabaseStatus,
    OverwritePolicy,
    DecryptionMethod,
)
from .events import DownloadEvent, DownloadEventCallback, DownloadStage, EventState
from .database import MediaDatabase
from .playlist import PlaylistWriter
from .paths import PathFormatter, PlaylistTags
from .exceptions import DownloadFinalizationError, TagResolutionError
from .tag_resolver import TagResolver
from .tags import Tags

__all__ = [
    "Downloader",
    "DownloadProgress",
    "DownloadResult",
    "FileResult",
    "PlaylistResult",
    "DatabaseResult",
    "MediaStatus",
    "FileStatus",
    "PlaylistStatus",
    "DatabaseStatus",
    "OverwritePolicy",
    "DecryptionMethod",
    "DownloadEvent",
    "DownloadEventCallback",
    "DownloadStage",
    "EventState",
    "MediaDatabase",
    "PlaylistWriter",
    "PathFormatter",
    "PlaylistTags",
    "DownloadFinalizationError",
    "Tags",
    "TagResolver",
    "TagResolutionError",
]
