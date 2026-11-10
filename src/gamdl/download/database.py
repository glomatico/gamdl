"""Optional SQLite media index; never owns or modifies downloaded media files."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gamdl.media import Media

    from .downloader import DownloadResult
from .enums import DatabaseStatus


class MediaDatabase:
    """Index published files by their path and originating Apple Music media.

    Recording an existing file is idempotent. A file may occur in multiple
    playlists; those occurrences are stored in playlist files, not this index.
    Each operation uses a short SQLite transaction so a failed update can be
    retried after download, including when the media file is already present.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lookup_index_checked = False
        self._lookup_index_lock = threading.Lock()

    def _ensure_lookup_index(self) -> None:
        """Add the ID lookup index to existing 4.x databases once per session."""
        if self._lookup_index_checked or not self.path.is_file():
            return
        with self._lookup_index_lock:
            if self._lookup_index_checked:
                return
            try:
                with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
                    db.execute(
                        "CREATE INDEX IF NOT EXISTS media_id_lookup_idx "
                        "ON media_files(media_type, media_id)"
                    )
            except sqlite3.OperationalError:
                # Older/incomplete or read-only indexes still permit lookup.
                pass
            self._lookup_index_checked = True

    def record(self, media: Media, result: DownloadResult) -> DatabaseStatus:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        source = media.source
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            # Serialize read/compare/write so two concurrent writers cannot
            # mistakenly both report an INSERTED result.
            db.execute("BEGIN IMMEDIATE")
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
            values = (
                str(result.path.absolute()),
                media.type,
                media.id,
                result.codec.value if result.codec is not None else None,
                result.path.stat().st_size,
                source.kind if source else None,
                source.id if source else None,
                source.index if source else None,
            )
            old = db.execute(
                """SELECT media_type, media_id, codec, file_size,
                          source_kind, source_id, source_index
                   FROM media_files WHERE path = ?""",
                (values[0],),
            ).fetchone()
            if old is not None:
                codec = (
                    values[3]
                    if values[3] is not None
                    else old[2]
                    if old[:2] == values[1:3]
                    else None
                )
                values = (
                    *values[:3],
                    codec,
                    values[4],
                    values[5] if values[5] is not None else old[4],
                    values[6] if values[6] is not None else old[5],
                    values[7] if values[7] is not None else old[6],
                )
                if old == values[1:]:
                    return DatabaseStatus.UNCHANGED
            db.execute(
                """INSERT INTO media_files (
                    path, media_type, media_id, codec, file_size,
                    source_kind, source_id, source_index
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    media_type = excluded.media_type,
                    codec = excluded.codec,
                    media_id = excluded.media_id,
                    file_size = excluded.file_size,
                    source_kind = excluded.source_kind,
                    source_id = excluded.source_id,
                    source_index = excluded.source_index,
                    updated_at = CURRENT_TIMESTAMP""",
                values,
            )
            return DatabaseStatus.INSERTED if old is None else DatabaseStatus.UPDATED

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

    def find_by_media_id(
        self,
        media_type: str,
        media_id: str,
        *,
        root: Path | None = None,
        extensions: tuple[str, ...] | None = None,
        codec: str | None = None,
    ) -> dict[str, object] | None:
        """Find a usable local copy before any network-dependent media work.

        A catalog ID can have several paths (including different codecs).
        Ignore stale records and files outside the requested output root.  An
        explicit codec constraint requires recorded codec provenance; records
        created by an offline database scan normally have a null codec.
        """
        if not self.path.is_file():
            return None
        self._ensure_lookup_index()
        root = root.absolute() if root is not None else None
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            try:
                rows = db.execute(
                    "SELECT * FROM media_files WHERE media_type = ? AND media_id = ? "
                    "ORDER BY updated_at DESC, path",
                    (media_type, str(media_id)),
                ).fetchall()
            except sqlite3.OperationalError:
                return None
        for row in rows:
            record = dict(row)
            path = Path(record["path"])
            if root is not None and not path.resolve().is_relative_to(root.resolve()):
                continue
            if extensions is not None and path.suffix.lower() not in extensions:
                continue
            if codec is not None and record["codec"] != codec:
                continue
            try:
                if path.is_symlink():
                    continue
                stat = path.stat()
            except OSError:
                continue
            if (
                not path.is_file()
                or stat.st_size <= 0
                or stat.st_size != record["file_size"]
            ):
                continue
            # The DB media ID is the identity authority for this fast path.
            # Reading MP4 tags for every hit can be expensive for large videos.
            # Reject missing or changed-size files instead of repeatedly
            # parsing their containers (database scan can rebuild stale IDs).
            return record
        return None

    def local_records(self, root: Path) -> list[dict[str, object]]:
        """Read local index entries in bulk for commands such as playlist sync."""
        if not self.path.is_file():
            return []
        root = root.absolute()
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            try:
                rows = db.execute(
                    "SELECT path, media_type, media_id, file_size FROM media_files"
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(row) for row in rows if Path(row["path"]).is_relative_to(root)]

    def relocate(self, source: Path, destination: Path) -> bool:
        """Update an existing path without losing its codec or source metadata.

        Do not create a database when organization is being run offline.
        A different record at the destination is never overwritten.
        """
        if not self.path.is_file():
            return False
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT 1 FROM media_files WHERE path = ?", (str(source.absolute()),)
            ).fetchone()
            if old is None:
                return False
            if db.execute(
                "SELECT 1 FROM media_files WHERE path = ?",
                (str(destination.absolute()),),
            ).fetchone():
                raise FileExistsError(f"Media index already contains {destination}")
            db.execute(
                """UPDATE media_files
                   SET path = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE path = ?""",
                (str(destination.absolute()), str(source.absolute())),
            )
            return True

    def index_file(self, path: Path, media_type: str, media_id: str) -> DatabaseStatus:
        """Reconcile an existing file without requiring Apple Music API objects."""
        path = path.absolute()
        size = path.stat().st_size
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """CREATE TABLE IF NOT EXISTS media_files (
                    path TEXT PRIMARY KEY, media_type TEXT NOT NULL,
                    media_id TEXT NOT NULL, codec TEXT,
                    file_size INTEGER NOT NULL, source_kind TEXT,
                    source_id TEXT, source_index INTEGER,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            previous = db.execute(
                "SELECT media_type, media_id, file_size FROM media_files WHERE path = ?",
                (str(path),),
            ).fetchone()
            if previous == (media_type, media_id, size):
                return DatabaseStatus.UNCHANGED
            db.execute(
                """INSERT INTO media_files (path, media_type, media_id, file_size)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    media_type = excluded.media_type,
                    media_id = excluded.media_id,
                    file_size = excluded.file_size,
                    codec = CASE WHEN media_files.media_id = excluded.media_id
                        AND media_files.media_type = excluded.media_type
                        THEN media_files.codec ELSE NULL END,
                    source_kind = CASE WHEN media_files.media_id = excluded.media_id
                        AND media_files.media_type = excluded.media_type
                        THEN media_files.source_kind ELSE NULL END,
                    source_id = CASE WHEN media_files.media_id = excluded.media_id
                        AND media_files.media_type = excluded.media_type
                        THEN media_files.source_id ELSE NULL END,
                    source_index = CASE WHEN media_files.media_id = excluded.media_id
                        AND media_files.media_type = excluded.media_type
                        THEN media_files.source_index ELSE NULL END,
                    updated_at = CURRENT_TIMESTAMP""",
                (str(path), media_type, media_id, size),
            )
            return (
                DatabaseStatus.INSERTED if previous is None else DatabaseStatus.UPDATED
            )
