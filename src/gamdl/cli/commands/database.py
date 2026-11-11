"""Offline SQLite index maintenance, metadata recovery and source reconstruction."""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

import click
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)

from gamdl import _native
from gamdl.cli.output import console
from gamdl.download.database import MediaDatabase, _source_rank
from gamdl.download.playlist import split_playlist
from gamdl.sync.playlists import PlaylistSnapshot, PlaylistSyncError


@click.group("database")
def database_group() -> None:
    """Inspect and reconcile the local media index."""


def _settings(ctx: click.Context):
    settings = ctx.find_root().obj["settings"]
    if settings.database.disabled:
        click.echo(
            "Warning: database tracking is disabled (database.disabled: true). "
            "Manual maintenance commands still work, but downloads will not update the index.",
            err=True,
        )
    return settings


def _database(settings, *, directory: Path | None = None) -> MediaDatabase:
    configured = settings.output.directory
    if configured is None and directory is None:
        raise click.ClickException(
            "Output directory is not configured. Run a download to choose one, "
            "set output.directory in the config, or use --directory with database scan."
        )
    return MediaDatabase(
        Path(settings.database.path).expanduser(),
        root=Path(configured).expanduser() if configured is not None else directory,
        save_metadata=settings.database.save_metadata,
        on_created=lambda path: click.echo(f"Created database at {path}"),
    )


def _rows(db: MediaDatabase) -> list[dict]:
    if not db.path.is_file():
        return []
    with closing(db._read()) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM media_files")]


def _existing(db: MediaDatabase) -> dict[Path, dict]:
    return {db.decode_path(str(row["path"])): row for row in _rows(db)}


@database_group.command("info")
@click.pass_context
def info(ctx: click.Context) -> None:
    """Show index location, size and missing-file count."""
    settings = _settings(ctx)
    path = Path(settings.database.path).expanduser()
    click.echo(f"Database: {path}")
    if not path.is_file():
        click.echo("Database does not exist yet.")
        return
    if settings.output.directory is None:
        click.echo(
            "Output directory is not configured; cannot check indexed media paths."
        )
        return
    db = _database(settings)
    try:
        rows = _rows(db)
        missing = sum(not db.decode_path(str(row["path"])).is_file() for row in rows)
    except (sqlite3.Error, ValueError) as error:
        raise click.ClickException(f"Unable to read media index: {error}") from None
    click.echo(f"Records: {len(rows)}")
    click.echo(f"Missing files: {missing}")
    click.echo(f"Database size: {db.path.stat().st_size} bytes")


def _read_file_tags(path: Path) -> dict:
    return dict(
        _native.read_mp3_organize_tags(str(path))
        if path.suffix.lower() == ".mp3"
        else _native.read_mp4_organize_tags(str(path))
    )


def _media_type(path: Path, previous: dict | None) -> str | None:
    suffix = path.suffix.lower()
    if previous is not None:
        return str(previous["media_type"])
    if suffix in {".mp3", ".m4a"}:
        return "songs"
    if suffix == ".m4v":
        return "music-videos"
    # MP4 is shared by many types; never guess from the extension alone.
    return None


def _scan_one(
    path: Path,
    previous: dict | None,
    db: MediaDatabase,
    counts: Counter,
    dry_run: bool,
) -> tuple[str, str, dict] | None:
    counts["scanned"] += 1
    kind = _media_type(path, previous)
    if kind is None:
        counts["ambiguous"] += 1
        return None
    try:
        try:
            tags = _read_file_tags(path)
        except (OSError, ValueError, TypeError):
            # A file can carry an identifiable cnID even when the optional
            # extended tag reader cannot inspect the remaining metadata.
            tags = {}
        media_id = tags.get("title_id")
        if not media_id and path.suffix.lower() != ".mp3":
            media_id = _native.read_mp4_title_id(str(path))
        if (
            not media_id
            and previous is not None
            and previous["file_size"] == path.stat().st_size
        ):
            media_id = previous["media_id"]
        if not media_id:
            counts["untagged"] += 1
            return None
        media_id = str(media_id)
        if previous is not None and (
            previous["media_type"],
            str(previous["media_id"]),
            previous["file_size"],
        ) == (kind, media_id, path.stat().st_size):
            counts["unchanged"] += 1
        elif dry_run:
            counts["updated" if previous else "inserted"] += 1
        else:
            status = db.index_file(path, kind, media_id)
            counts[status.value] += 1
        if db.save_metadata:
            # An unchanged file may still lack a type-specific metadata row.
            old_tags = db.get_metadata(path, kind) if previous is not None else None
            new_values = {
                key: value
                for key, value in tags.items()
                if value is not None and (old_tags is None or old_tags.get(key) is None)
            }
            if new_values and (dry_run or db.upsert_metadata(path, kind, new_values)):
                counts["metadata"] += 1
        return kind, media_id, tags
    except (OSError, ValueError, TypeError, sqlite3.Error) as error:
        counts["errors"] += 1
        click.echo(f"Cannot index {path}: {error}", err=True)
        return None


def _promote(
    path: Path,
    kind: str,
    source_id: str,
    index: int | None,
    sources: dict[Path, tuple[str, str | None, int | None]],
    db: MediaDatabase,
    dry_run: bool,
    counts: Counter,
) -> None:
    previous = sources.get(path)
    if previous and _source_rank(previous[0]) >= _source_rank(kind):
        return
    sources[path] = (kind, source_id, index)
    counts["source_" + kind] += 1
    if not dry_run:
        db.update_source(path, kind, source_id, index)


def _reconstruct_sources(
    root: Path,
    catalog: dict[Path, tuple[str, str, dict]],
    existing: dict[Path, dict],
    db: MediaDatabase,
    dry_run: bool,
    counts: Counter,
) -> None:
    sources: dict[Path, tuple[str, str | None, int | None]] = {
        path: (str(row["source_kind"]), row["source_id"], row["source_index"])
        for path, row in existing.items()
        if row["source_kind"] is not None
    }
    for path, (kind, media_id, _) in catalog.items():
        if kind != "uploaded-videos" and path not in sources:
            _promote(path, "single_track", media_id, None, sources, db, dry_run, counts)

    for playlist in sorted(root.rglob("*.m3u8")):
        try:
            snapshot = PlaylistSnapshot.read(playlist, root)
        except (PlaylistSyncError, OSError, UnicodeError, ValueError) as error:
            counts["invalid_playlists"] += 1
            click.echo(f"Skipping invalid playlist {playlist}: {error}", err=True)
            continue
        # An M3U can contain empty positional placeholders. They count toward
        # the original playlist index even though Snapshot.entries omits them.
        _, body = split_playlist(snapshot.original.decode("utf-8-sig").splitlines())
        offsets = []
        position = 0
        for line in body:
            if line.startswith("#"):
                continue
            if line.strip():
                offsets.append(position)
            position += 1
        for i, entry in zip(offsets, snapshot.entries):
            if entry.path in catalog:
                _promote(
                    entry.path,
                    "playlist_tracks",
                    snapshot.playlist_id,
                    i,
                    sources,
                    db,
                    dry_run,
                    counts,
                )

    albums: dict[str, list[tuple[Path, str, dict]]] = defaultdict(list)
    for path, (kind, media_id, tags) in catalog.items():
        if kind in {"songs", "library-songs"} and tags.get("album_id"):
            albums[str(tags["album_id"])].append((path, media_id, tags))

    for album_id, records in albums.items():
        positions: dict[tuple[int, int], str] = {}
        total_discs: set[int] = set()
        totals: dict[int, set[int]] = defaultdict(set)
        valid = True
        for _, media_id, tags in records:
            try:
                disc = int(tags.get("disc_number") or 1)
                track = int(tags["track_number"])
                track_total = int(tags["track_total"])
                disc_total = int(tags.get("disc_total") or 1)
                if not (
                    disc > 0
                    and track > 0
                    and track_total > 0
                    and 1 <= disc <= disc_total
                    and track <= track_total
                ):
                    valid = False
                    break
            except (TypeError, ValueError, KeyError):
                valid = False
                break
            total_discs.add(disc_total)
            totals[disc].add(track_total)
            previous_id = positions.setdefault((disc, track), media_id)
            if previous_id != media_id:
                valid = False
                break
        if not valid or len(total_discs) != 1:
            counts["incomplete_albums"] += 1
            continue
        disc_count = next(iter(total_discs))
        if (
            set(totals) != set(range(1, disc_count + 1))
            or any(len(values) != 1 for values in totals.values())
            or any(
                {track for d, track in positions if d == disc}
                != set(range(1, next(iter(totals[disc])) + 1))
                for disc in totals
            )
        ):
            counts["incomplete_albums"] += 1
            continue
        counts["complete_albums"] += 1
        ordered = {position: i for i, position in enumerate(sorted(positions))}
        for path, _, tags in records:
            position = (int(tags.get("disc_number") or 1), int(tags["track_number"]))
            _promote(
                path,
                "album_tracks",
                album_id,
                ordered[position],
                sources,
                db,
                dry_run,
                counts,
            )


@database_group.command("scan")
@click.option(
    "--directory",
    type=click.Path(path_type=Path, file_okay=False),
    help="Directory to scan (defaults to configured output.directory)",
)
@click.option("--dry-run", is_flag=True, help="Preview changes without writing SQLite")
@click.pass_context
def scan(ctx: click.Context, directory: Path | None, dry_run: bool) -> None:
    """Index local media, recover tags, playlists and complete album membership."""
    settings = _settings(ctx)
    db = _database(settings, directory=directory)
    root = (directory or db.root).expanduser().resolve()
    if not root.is_dir():
        raise click.ClickException(f"Output directory does not exist: {root}")
    if not root.is_relative_to(db.root):
        raise click.ClickException("Scan directory must be under output.directory")
    try:
        existing = _existing(db)
    except (sqlite3.Error, ValueError) as error:
        raise click.ClickException(f"Unable to read media index: {error}") from None

    counts: Counter[str] = Counter()
    extensions = {".mp3", ".m4a", ".m4v", ".mp4"}

    def candidates():
        return (
            path
            for path in root.rglob("*")
            if not path.is_symlink()
            and path.is_file()
            and path.suffix.lower() in extensions
        )

    with Progress(
        SpinnerColumn(),
        TextColumn("Counting media files"),
        console=console(stderr=True),
        transient=True,
    ) as progress:
        task = progress.add_task("Counting media files", total=None)
        total = 0
        for _ in candidates():
            total += 1
            progress.advance(task)
    catalog: dict[Path, tuple[str, str, dict]] = {}
    with Progress(
        SpinnerColumn(),
        TextColumn("Scanning media"),
        BarColumn(bar_width=24),
        TaskProgressColumn(),
        console=console(stderr=True),
        transient=True,
    ) as progress:
        task = progress.add_task("Scanning media", total=total)
        for path in candidates():
            resolved = _scan_one(path, existing.get(path), db, counts, dry_run)
            if resolved is not None:
                catalog[path] = resolved
            progress.advance(task)
    try:
        _reconstruct_sources(root, catalog, existing, db, dry_run, counts)
    except (sqlite3.Error, ValueError) as error:
        raise click.ClickException(f"Cannot reconstruct sources: {error}") from None

    click.echo(
        ("Dry run: " if dry_run else "")
        + f"Scanned {counts['scanned']}; added {counts['inserted']}, "
        f"updated {counts['updated']}, unchanged {counts['unchanged']}, "
        f"untagged {counts['untagged']}, ambiguous {counts['ambiguous']}, "
        f"metadata filled {counts['metadata']}, "
        f"playlists {counts['source_playlist_tracks']}, "
        f"complete albums {counts['complete_albums']}, "
        f"incomplete albums {counts['incomplete_albums']}, "
        f"errors {counts['errors']}."
    )
    if counts["errors"]:
        raise SystemExit(1)


@database_group.command("prune")
@click.option("--dry-run", is_flag=True, help="Preview removals")
@click.option("--yes", is_flag=True, help="Confirm deletion of missing-file records")
@click.pass_context
def prune(ctx: click.Context, dry_run: bool, yes: bool) -> None:
    """Remove index rows for missing files; never delete media files."""
    db = _database(_settings(ctx))
    try:
        missing = [
            str(row["path"])
            for row in _rows(db)
            if not db.decode_path(str(row["path"])).is_file()
        ]
    except (sqlite3.Error, ValueError) as error:
        raise click.ClickException(f"Unable to read media index: {error}") from None
    click.echo(f"Missing-file records: {len(missing)}")
    if not missing or dry_run:
        return
    if not yes:
        click.confirm("Remove these database records?", abort=True)
    with closing(db._read()) as conn, conn:
        conn.executemany(
            "DELETE FROM media_files WHERE path=?", [(x,) for x in missing]
        )
    click.echo(f"Removed {len(missing)} records.")
