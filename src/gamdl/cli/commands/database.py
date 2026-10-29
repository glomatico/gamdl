"""Offline SQLite index maintenance; never changes downloaded media."""

import sqlite3
from collections import Counter
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
from gamdl.download.database import MediaDatabase


@click.group("database")
def database_group() -> None:
    """Inspect and reconcile the local media index."""


def _settings(ctx: click.Context):
    settings = ctx.find_root().obj["settings"]
    if not settings.database.enabled:
        click.echo(
            "Warning: database tracking is disabled (database.enabled: false). "
            "These commands still work, but downloads will not update the index.\n"
            "Enable database.enabled in your GAMDL YAML configuration.",
            err=True,
        )
    return settings


def _database(settings) -> MediaDatabase:
    return MediaDatabase(Path(settings.database.path).expanduser())


def _rows(db: MediaDatabase):
    if not db.path.is_file():
        return []
    with closing(sqlite3.connect(db.path, timeout=10)) as conn:
        return conn.execute(
            "SELECT path, media_type, media_id, file_size FROM media_files"
        ).fetchall()


@database_group.command("info")
@click.pass_context
def info(ctx: click.Context) -> None:
    """Show index location, size and missing-file count."""
    db = _database(_settings(ctx))
    click.echo(f"Database: {db.path.expanduser().absolute()}")
    if not db.path.is_file():
        click.echo("Database does not exist yet.")
        return
    try:
        rows = _rows(db)
    except sqlite3.Error as error:
        raise click.ClickException(f"Unable to read media index: {error}") from None
    click.echo(f"Records: {len(rows)}")
    click.echo(f"Missing files: {sum(not Path(row[0]).is_file() for row in rows)}")
    click.echo(f"Database size: {db.path.stat().st_size} bytes")


@database_group.command("refresh")
@click.option(
    "--directory",
    type=click.Path(path_type=Path, file_okay=False),
    help="Directory to scan (defaults to configured output.directory)",
)
@click.option("--dry-run", is_flag=True, help="Preview changes without writing SQLite")
@click.pass_context
def refresh(ctx: click.Context, directory: Path | None, dry_run: bool) -> None:
    """Scan existing files and reconcile cnID-based index entries."""
    settings = _settings(ctx)
    db = _database(settings)
    root = (directory or Path(settings.output.directory)).expanduser().absolute()
    if not root.is_dir():
        raise click.ClickException(f"Output directory does not exist: {root}")
    try:
        existing = {row[0]: row for row in _rows(db)}
    except sqlite3.Error as error:
        raise click.ClickException(f"Unable to read media index: {error}") from None
    counts: Counter[str] = Counter()
    # Counting first gives the scan an accurate total instead of a misleading
    # indeterminate bar. Keep only the count, not a list of every media path.
    extensions = {".m4a", ".m4v", ".mp4"}

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
    ) as counting:
        counter = counting.add_task("Counting media files", total=None)
        total = 0
        for _ in candidates():
            total += 1
            counting.advance(counter)

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
            _refresh_file(path, existing, counts, db, dry_run)
            progress.advance(task)
    click.echo(
        ("Dry run: " if dry_run else "") + f"Scanned {counts['scanned']}; "
        f"added {counts['inserted']}, updated {counts['updated']}, "
        f"unchanged {counts['unchanged']}, untagged {counts['untagged']}, "
        f"ambiguous {counts['ambiguous']}, errors {counts['errors']}."
    )
    if counts["errors"]:
        raise SystemExit(1)


def _refresh_file(
    path: Path, existing: dict, counts: Counter[str], db: MediaDatabase, dry_run: bool
) -> None:
    counts["scanned"] += 1
    previous = existing.get(str(path.absolute()))
    # MP4 is ambiguous without an existing record. Never invent a media type.
    media_type = (
        "songs"
        if path.suffix.lower() == ".m4a"
        else "music-videos"
        if path.suffix.lower() == ".m4v"
        else previous[1]
        if previous
        else None
    )
    if media_type is None:
        counts["ambiguous"] += 1
        return
    try:
        media_id = _native.read_mp4_title_id(str(path))
        if not media_id or not str(media_id).isdigit():
            counts["untagged"] += 1
            return
        if (
            previous
            and previous[1] == media_type
            and previous[2] == str(media_id)
            and previous[3] == path.stat().st_size
        ):
            counts["unchanged"] += 1
            return
        if dry_run:
            counts["updated" if previous else "inserted"] += 1
        else:
            status = db.index_file(path, media_type, str(media_id))
            counts[status.value] += 1
    except (OSError, ValueError, sqlite3.Error) as error:
        counts["errors"] += 1
        click.echo(f"Cannot index {path}: {error}", err=True)


@database_group.command("prune")
@click.option("--dry-run", is_flag=True, help="Preview removals")
@click.option("--yes", is_flag=True, help="Confirm deletion of missing-file records")
@click.pass_context
def prune(ctx: click.Context, dry_run: bool, yes: bool) -> None:
    """Remove index records for missing files; never delete media files."""
    db = _database(_settings(ctx))
    try:
        missing = [row[0] for row in _rows(db) if not Path(row[0]).is_file()]
    except sqlite3.Error as error:
        raise click.ClickException(f"Unable to read media index: {error}") from None
    click.echo(f"Missing-file records: {len(missing)}")
    if not missing or dry_run:
        return
    if not yes:
        click.confirm("Remove these database records?", abort=True)
    with closing(sqlite3.connect(db.path, timeout=10)) as conn, conn:
        conn.executemany(
            "DELETE FROM media_files WHERE path = ?", [(path,) for path in missing]
        )
    click.echo(f"Removed {len(missing)} records.")
