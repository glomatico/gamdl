"""Optional SQLite media index; never owns or modifies downloaded media files."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gamdl.media import Media
    from .downloader import DownloadResult


class MediaDatabase:
    """Index published files by their path and originating Apple Music media.

    Recording an existing file is idempotent. A file may occur in multiple
    playlists; those occurrences are stored in playlist files, not this index.
    Each operation uses a short SQLite transaction so a failed update can be
    retried after download, including when the media file is already present.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def record(self, media: Media, result: DownloadResult) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        source = media.source
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS media_files (
                    path TEXT PRIMARY KEY,
                    media_type TEXT NOT NULL,
                    media_id TEXT NOT NULL,
                    codec TEXT,
                    file_size INTEGER NOT NULL,
                    source_kind TEXT,
                    source_id TEXT,
                    source_index INTEGER,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            db.execute(
                """INSERT INTO media_files (
                    path, media_type, media_id, codec, file_size,
                    source_kind, source_id, source_index
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    media_type = excluded.media_type,
                    codec = CASE WHEN media_files.media_type = excluded.media_type
                                 AND media_files.media_id = excluded.media_id
                            THEN COALESCE(excluded.codec, media_files.codec)
                            ELSE excluded.codec END,
                    media_id = excluded.media_id,
                    file_size = excluded.file_size,
                    source_kind = COALESCE(excluded.source_kind, media_files.source_kind),
                    source_id = COALESCE(excluded.source_id, media_files.source_id),
                    source_index = COALESCE(excluded.source_index, media_files.source_index),
                    updated_at = CURRENT_TIMESTAMP""",
                (
                    str(result.path.absolute()),
                    media.type,
                    media.id,
                    result.codec.value if result.codec is not None else None,
                    result.path.stat().st_size,
                    source.kind if source else None,
                    source.id if source else None,
                    source.index if source else None,
                ),
            )

    def get(self, path: str | Path) -> dict[str, object] | None:
        """Read a stored record; return None when no database/record exists."""
        if not self.path.is_file():
            return None
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM media_files WHERE path = ?",
                (str(Path(path).absolute()),),
            ).fetchone()
        return dict(row) if row is not None else None
