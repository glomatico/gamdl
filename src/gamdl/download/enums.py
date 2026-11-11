"""Stable string-valued statuses and policies for downloader consumers."""

from enum import Enum


class MediaStatus(str, Enum):
    DOWNLOADED = "downloaded"
    OVERWRITTEN = "overwritten"
    SKIPPED = "skipped"


class FileStatus(str, Enum):
    CREATED = "created"
    OVERWRITTEN = "overwritten"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"


class PlaylistStatus(str, Enum):
    UPDATED = "updated"
    UNCHANGED = "unchanged"


class DatabaseStatus(str, Enum):
    INSERTED = "inserted"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"


class OverwritePolicy(str, Enum):
    SKIP = "skip"
    REPLACE = "replace"


class DecryptionMethod(str, Enum):
    NONE = "none"
    EXPLICIT_KEY = "explicit_key"
    WIDEVINE = "widevine"
    PLAYREADY = "playready"
    WRAPPER = "wrapper"
