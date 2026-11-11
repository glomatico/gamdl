"""Portable SQLite media catalog, with optional separately stored resolved tags."""

from __future__ import annotations

import json
import sqlite3
import threading
import warnings
from contextlib import closing
from dataclasses import fields
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .enums import DatabaseStatus
from .tags import Tags

if TYPE_CHECKING:
    from gamdl.media import Media
    from .models import DownloadResult

SCHEMA_VERSION = 1
TAG_NAMES = tuple(field.name for field in fields(Tags) if field.name != "lyrics")
UPLOADED_VIDEO_TAGS = (
    "title", "artist", "title_id", "release_date", "storefront", "media_type"
)
TAG_TABLES = {
    "songs": TAG_NAMES,
    "music_videos": TAG_NAMES,
    "uploaded_videos": UPLOADED_VIDEO_TAGS,
}
IDENTIFIER_TAGS = frozenset({"title_id", "artist_id", "album_id", "composer_id", "genre_id"})
INTEGER_TAGS = frozenset({
    "track_number", "track_total", "disc_number", "disc_total",
    "compilation", "gapless", "explicit", "storefront", "media_type", "rating",
})
KIND_TABLES = {
    "songs": "songs", "library-songs": "songs",
    "music-videos": "music_videos", "library-music-videos": "music_videos",
    "uploaded-videos": "uploaded_videos",
}


def _type_sql(name: str) -> str:
    if name in INTEGER_TAGS:
        return "INTEGER"
    return "TEXT"


def _table_sql(table: str) -> str:
    columns = []
    for name in TAG_TABLES[table]:
        constraint = (
            " CHECK (" + name + " IN (0, 1) OR " + name + " IS NULL)"
            if name in {"compilation", "gapless", "explicit"} else ""
        )
        columns.append(f'    "{name}" {_type_sql(name)}{constraint}')
    return (
        f"CREATE TABLE IF NOT EXISTS {table} (\n"
        "    file_id INTEGER PRIMARY KEY REFERENCES media_files(file_id) ON DELETE CASCADE,\n"
        + ",\n".join(columns) + "\n)"
    )


MEDIA_FILES_SQL = """CREATE TABLE media_files (
    file_id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    media_id TEXT NOT NULL,
    codec TEXT,
    file_size INTEGER NOT NULL CHECK (file_size >= 0),
    source_kind TEXT,
    source_id TEXT,
    source_index INTEGER,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)"""


def _source_rank(kind: str | None) -> int:
    if kind == "album_tracks":
        return 2
    if kind == "playlist_tracks":
        return 1
    return 0


def _source(media: Media) -> tuple[str | None, str | None, int | None]:
    if media.type == "uploaded-videos":
        return (None, None, None)
    source = media.source
    if source is None:
        return ("single_track", str(media.id), None)
    return (source.kind, str(source.id), source.index)


def _serialize_tags(tags: Tags | dict[str, object], names: tuple[str, ...]) -> dict[str, object]:
    data = {name: getattr(tags, name, None) for name in names} if isinstance(tags, Tags) else tags
    values: dict[str, object] = {}
    for name in names:
        value = data.get(name)
        if name == "genre":
            if isinstance(value, str):
                # Local readers return a single genre string; stored format is an array.
                value = (value,) if value else ()
            value = json.dumps(list(value), ensure_ascii=False) if value else None
        elif name in IDENTIFIER_TAGS and value is not None:
            value = str(value)
        elif name in INTEGER_TAGS and value is not None:
            value = int(value)
        values[name] = value
    return values


class MediaDatabase:
    """One SQLite catalog for one output root; stored paths are always relative."""

    def __init__(
        self,
        path: str | Path,
        *,
        root: str | Path | None = None,
        save_metadata: bool = False,
        on_created: Callable[[Path], None] | None = None,
    ) -> None:
        self.path = Path(path).expanduser().absolute()
        # Programmatic callers should specify a root; retaining path.parent as
        # a conservative fallback avoids assuming a global working directory.
        self.root = Path(root).expanduser().resolve() if root is not None else self.path.parent.resolve()
        self.save_metadata = save_metadata
        self.on_created = on_created
        self._initialized = False
        self._init_lock = threading.Lock()

    def encode_path(self, path: str | Path) -> str:
        candidate = Path(path).expanduser().absolute()
        try:
            relative = candidate.relative_to(self.root)
        except ValueError:
            raise ValueError(f"Media is outside the database output directory: {candidate}") from None
        if not relative.parts or any(part in {".", ".."} for part in relative.parts):
            raise ValueError(f"Unsafe database media path: {candidate}")
        if candidate.resolve() != candidate or candidate.is_symlink():
            raise ValueError(f"Symlinked media is not indexable: {candidate}")
        return relative.as_posix()

    def decode_path(self, value: str) -> Path:
        if not value or "\\" in value:
            raise ValueError("Invalid relative database path")
        relative = Path(value)
        if relative.is_absolute() or any(part in {".", ".."} for part in value.split("/")):
            raise ValueError(f"Invalid relative database path: {value!r}")
        path = self.root / relative
        if not path.resolve().is_relative_to(self.root) or path.is_symlink():
            raise ValueError(f"Unsafe indexed media path: {value!r}")
        return path

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path, timeout=10)
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _validate(db: sqlite3.Connection) -> None:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_files'"
        ).fetchone()
        if version != SCHEMA_VERSION or not exists:
            raise ValueError(
                "Unsupported Gamdl database schema. This release requires a fresh "
                "database; move the old SQLite file aside or change database.path. "
                "The existing database has not been modified."
            )

    def _read(self) -> sqlite3.Connection | None:
        if not self.path.is_file():
            return None
        db = self._connect(self.path)
        try:
            self._validate(db)
        except BaseException:
            db.close()
            raise
        return db

    def initialize(self) -> bool:
        """Create a fresh schema, or validate an existing database without migration."""
        with self._init_lock:
            if self._initialized and self.path.is_file():
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            created = False
            with closing(self._connect(self.path)) as db:
                # Validate before changing the journal mode of an old SQLite
                # database. An incompatible database must remain untouched.
                existing = db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                if existing:
                    self._validate(db)
                db.execute("PRAGMA journal_mode=WAL")
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    existing = db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                    if not existing:
                        db.execute(MEDIA_FILES_SQL)
                        db.execute(
                            "CREATE INDEX media_id_lookup_idx "
                            "ON media_files(media_type, media_id)"
                        )
                        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                        created = True
                    else:
                        self._validate(db)
            self._initialized = True
        if created and self.on_created is not None:
            self.on_created(self.path)
        return created

    def _file_record(self, row: sqlite3.Row) -> dict[str, object]:
        result = dict(row)
        result["path"] = str(self.decode_path(str(result["path"])))
        return result

    def get(self, path: str | Path) -> dict[str, object] | None:
        if not self.path.is_file():
            return None
        relative = self.encode_path(path)
        with closing(self._read()) as db:
            row = db.execute("SELECT * FROM media_files WHERE path=?", (relative,)).fetchone()
        return self._file_record(row) if row is not None else None

    def find_by_media_id(
        self,
        media_type: str,
        media_id: str,
        *,
        root: Path | None = None,
        extensions: tuple[str, ...] | None = None,
        codec: str | None = None,
    ) -> dict[str, object] | None:
        if not self.path.is_file():
            return None
        requested_root = Path(root).resolve() if root is not None else self.root
        with closing(self._read()) as db:
            rows = db.execute(
                "SELECT * FROM media_files WHERE media_type=? AND media_id=? "
                "ORDER BY updated_at DESC, path", (media_type, str(media_id)),
            ).fetchall()
        for row in rows:
            try:
                record = self._file_record(row)
            except ValueError:
                continue
            path = Path(str(record["path"]))
            if not path.is_relative_to(requested_root):
                continue
            if extensions is not None and path.suffix.lower() not in extensions:
                continue
            if codec is not None and record["codec"] != codec:
                continue
            try:
                stat = path.stat()
                if not path.is_file() or stat.st_size <= 0 or stat.st_size != record["file_size"]:
                    continue
            except OSError:
                continue
            return record
        return None

    def local_records(self, root: Path) -> list[dict[str, object]]:
        if not self.path.is_file():
            return []
        requested_root = Path(root).resolve()
        with closing(self._read()) as db:
            rows = db.execute("SELECT path, media_type, media_id, file_size FROM media_files").fetchall()
        records = []
        for row in rows:
            try:
                record = self._file_record(row)
                if Path(str(record["path"])).is_relative_to(requested_root):
                    records.append(record)
            except ValueError:
                continue
        return records

    def _write_index(
        self,
        db: sqlite3.Connection,
        path: str,
        media_type: str,
        media_id: str,
        file_size: int,
        *,
        codec: str | None = None,
        source: tuple[str | None, str | None, int | None] | None = None,
    ) -> DatabaseStatus:
        old = db.execute("SELECT * FROM media_files WHERE path=?", (path,)).fetchone()
        same = old is not None and (old["media_type"], str(old["media_id"])) == (media_type, str(media_id))
        previous_source = (
            (old["source_kind"], old["source_id"], old["source_index"])
            if same else (None, None, None)
        )
        incoming = source or (None, None, None)
        if source is None or (_source_rank(previous_source[0]) >= _source_rank(incoming[0]) and previous_source[0] is not None):
            chosen_source = previous_source
        else:
            chosen_source = incoming
        chosen_codec = codec if codec is not None else old["codec"] if same else None
        values = (
            path, media_type, str(media_id), chosen_codec, file_size, *chosen_source
        )
        if same and (
            old["codec"], old["file_size"], old["source_kind"],
            old["source_id"], old["source_index"]
        ) == (chosen_codec, file_size, *chosen_source):
            return DatabaseStatus.UNCHANGED
        if old is not None and not same:
            db.execute("DELETE FROM media_files WHERE file_id=?", (old["file_id"],))
        db.execute(
            """INSERT INTO media_files (
                path, media_type, media_id, codec, file_size,
                source_kind, source_id, source_index
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                media_type=excluded.media_type, media_id=excluded.media_id,
                codec=excluded.codec, file_size=excluded.file_size,
                source_kind=excluded.source_kind, source_id=excluded.source_id,
                source_index=excluded.source_index,
                updated_at=CURRENT_TIMESTAMP""",
            values,
        )
        return DatabaseStatus.INSERTED if old is None else DatabaseStatus.UPDATED

    def update_source(
        self, path: Path, kind: str, source_id: str, index: int | None = None
    ) -> DatabaseStatus:
        """Promote an indexed local item without touching its downloaded file."""
        relative = self.encode_path(path)
        if not self.path.is_file():
            raise ValueError("No database exists for source registration")
        with closing(self._read()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM media_files WHERE path=?", (relative,)).fetchone()
            if row is None:
                raise ValueError(f"Media is not indexed: {path}")
            if row["media_type"] == "uploaded-videos":
                return DatabaseStatus.UNCHANGED
            return self._write_index(
                db, relative, row["media_type"], row["media_id"], row["file_size"],
                codec=row["codec"], source=(kind, source_id, index),
            )

    def record(self, media: Media, result: DownloadResult, tags: Tags | None = None) -> DatabaseStatus:
        try:
            relative = self.encode_path(result.path)
        except ValueError as error:
            warnings.warn(str(error), stacklevel=2)
            return DatabaseStatus.UNCHANGED
        size = result.path.stat().st_size
        self.initialize()
        with closing(self._read()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            status = self._write_index(
                db, relative, media.type, str(media.id), size,
                codec=result.codec.value if result.codec is not None else None,
                source=_source(media),
            )
        if self.save_metadata and tags is not None:
            try:
                self.upsert_metadata(result.path, media.type, tags)
            except (sqlite3.Error, ValueError, TypeError, OverflowError) as error:
                warnings.warn(f"Could not save rich media metadata: {error}", stacklevel=2)
        return status

    def index_file(
        self, path: Path, media_type: str, media_id: str
    ) -> DatabaseStatus:
        relative = self.encode_path(path)
        size = path.stat().st_size
        self.initialize()
        with closing(self._read()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            return self._write_index(db, relative, media_type, str(media_id), size)

    def get_metadata(self, path: Path, media_type: str) -> dict[str, object] | None:
        table = KIND_TABLES.get(media_type)
        if not table or not self.path.is_file():
            return None
        relative = self.encode_path(path)
        with closing(self._read()) as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                return None
            row = db.execute(
                f"SELECT t.* FROM {table} t JOIN media_files m USING(file_id) "
                "WHERE m.path=? AND m.media_type=?", (relative, media_type),
            ).fetchone()
        return dict(row) if row is not None else None

    def upsert_metadata(
        self, path: Path, media_type: str, tags: Tags | dict[str, object]
    ) -> bool:
        """Fill missing tag fields; never overwrite a non-null cached value."""
        table = KIND_TABLES.get(media_type)
        if table is None:
            return False
        relative = self.encode_path(path)
        values = _serialize_tags(tags, TAG_TABLES[table])
        if not any(value is not None for value in values.values()):
            return False
        self.initialize()
        columns = list(values)
        with closing(self._read()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT file_id FROM media_files WHERE path=? AND media_type=?",
                (relative, media_type),
            ).fetchone()
            if row is None:
                return False
            db.execute(_table_sql(table))
            quoted = ", ".join(f'"{column}"' for column in columns)
            placeholders = ", ".join("?" for _ in columns)
            updates = ", ".join(
                f'"{col}"=COALESCE({table}."{col}", excluded."{col}")'
                for col in columns
            )
            previous = db.total_changes
            db.execute(
                f"INSERT INTO {table} (file_id, {quoted}) "
                f"VALUES (?, {placeholders}) "
                f"ON CONFLICT(file_id) DO UPDATE SET {updates}",
                (row["file_id"], *(values[name] for name in columns)),
            )
            return db.total_changes != previous

    def relocate(self, source: Path, destination: Path) -> bool:
        if not self.path.is_file():
            return False
        old_path = self.encode_path(source)
        new_path = self.encode_path(destination)
        with closing(self._read()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute(
                "SELECT 1 FROM media_files WHERE path=?", (old_path,)
            ).fetchone()
            if not exists:
                return False
            if db.execute("SELECT 1 FROM media_files WHERE path=?", (new_path,)).fetchone():
                raise FileExistsError(f"Media index already contains {destination}")
            db.execute(
                "UPDATE media_files SET path=?, updated_at=CURRENT_TIMESTAMP WHERE path=?",
                (new_path, old_path),
            )
        return True
