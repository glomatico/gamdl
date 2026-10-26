"""Immutable results of publishing media and optional finalization steps."""

from dataclasses import dataclass
from pathlib import Path

from gamdl.media import MusicVideoCodec, SongCodec, UploadedVideoQuality

from .enums import (
    DatabaseStatus,
    DecryptionMethod,
    FileStatus,
    MediaStatus,
    PlaylistStatus,
)


@dataclass(frozen=True)
class FileResult:
    path: Path | None
    status: FileStatus


@dataclass(frozen=True)
class PlaylistResult:
    path: Path
    status: PlaylistStatus


@dataclass(frozen=True)
class DatabaseResult:
    path: Path
    status: DatabaseStatus


@dataclass(frozen=True)
class DownloadResult:
    """Includes post-publication outcomes; no credentials or signed URLs."""

    media_id: str
    path: Path
    status: MediaStatus
    bytes_written: int
    codec: SongCodec | MusicVideoCodec | UploadedVideoQuality | None
    decryption_methods: tuple[DecryptionMethod, ...] = ()
    cover: FileResult | None = None
    lyrics: FileResult | None = None
    playlist_file: PlaylistResult | None = None
    database: DatabaseResult | None = None
