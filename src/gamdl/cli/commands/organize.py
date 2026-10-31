"""Offline, no-overwrite library organization by stored media tags."""

from __future__ import annotations

from pathlib import Path

import click
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)

from gamdl.cli.output import console
from gamdl.download.database import MediaDatabase
from gamdl.download.organizer import execute_organization, plan_organization
from gamdl.download.paths import PathFormatter


@click.command("organize")
@click.option(
    "--directory",
    type=click.Path(path_type=Path, file_okay=False),
    help="Library directory (defaults to configured output.directory)",
)
@click.option(
    "--dry-run", is_flag=True, help="Preview moves without changing files or SQLite"
)
@click.option("--album-folder-template", default=None)
@click.option("--compilation-folder-template", default=None)
@click.option("--no-album-folder-template", default=None)
@click.option("--single-disc-file-template", default=None)
@click.option("--multi-disc-file-template", default=None)
@click.option("--no-album-file-template", default=None)
@click.option("--playlist-folder-template", default=None)
@click.option("--playlist-file-template", default=None)
@click.option("--truncate", type=click.IntRange(min=1), default=None)
@click.pass_context
def organize(
    ctx: click.Context,
    directory: Path | None,
    dry_run: bool,
    album_folder_template: str | None,
    compilation_folder_template: str | None,
    no_album_folder_template: str | None,
    single_disc_file_template: str | None,
    multi_disc_file_template: str | None,
    no_album_file_template: str | None,
    playlist_folder_template: str | None,
    playlist_file_template: str | None,
    truncate: int | None,
) -> None:
    """Organize local songs/videos by their embedded tags and path templates.

    Only files inside the chosen library are considered. No network requests
    or media rewriting. Existing destinations and ambiguous files are skipped.
    """
    settings = ctx.find_root().obj["settings"]
    output = settings.output
    root = (directory or Path(output.directory)).expanduser().absolute()
    if not root.is_dir():
        raise click.ClickException(f"Output directory does not exist: {root}")
    values = {
        name: value if value is not None else getattr(output, name)
        for name, value in {
            "album_folder_template": album_folder_template,
            "compilation_folder_template": compilation_folder_template,
            "no_album_folder_template": no_album_folder_template,
            "single_disc_file_template": single_disc_file_template,
            "multi_disc_file_template": multi_disc_file_template,
            "no_album_file_template": no_album_file_template,
            "playlist_folder_template": playlist_folder_template,
            "playlist_file_template": playlist_file_template,
            "truncate": truncate,
        }.items()
    }
    db = MediaDatabase(Path(settings.database.path).expanduser())
    try:
        formatter = PathFormatter(root, **values)
        with Progress(
            SpinnerColumn(),
            TextColumn("Inspecting library metadata"),
            console=console(stderr=True),
            transient=True,
        ) as progress:
            progress.add_task("Inspecting", total=None)
            plan = plan_organization(root, formatter, db if db.path.is_file() else None)
    except (OSError, ValueError) as error:
        raise click.ClickException(f"Unable to plan organization: {error}") from None

    if dry_run:
        for move in plan.moves:
            click.echo(
                f"{move.source.relative_to(root)} -> {move.destination.relative_to(root)}"
            )
        for source, target in plan.playlist_destinations.items():
            click.echo(f"{source.relative_to(root)} -> {target.relative_to(root)}")
    for problem in plan.problems:
        click.echo(f"Warning: {problem}", err=True)

    if dry_run:
        counts = plan.counts
    else:
        with Progress(
            SpinnerColumn(),
            TextColumn("Moving library files"),
            BarColumn(bar_width=24),
            TaskProgressColumn(),
            console=console(stderr=True),
            transient=True,
        ) as progress:
            task = progress.add_task("Moving", total=len(plan.moves))
            # Progress corresponds to moves attempted. Execution also updates
            # relative playlists and recorded SQLite paths before returning.
            counts = execute_organization(
                plan,
                db,
                update_index=settings.database.enabled,
                on_move=lambda: progress.advance(task),
            )
        for problem in plan.problems:
            if problem.startswith(("Cannot move ", "Cannot update playlist ")):
                click.echo(f"Warning: {problem}", err=True)
    click.echo(
        ("Dry run: " if dry_run else "")
        + f"Scanned {counts['scanned']}; "
        + (f"would move {counts['planned']}" if dry_run else f"moved {counts['moved']}")
        + f", unchanged {counts['unchanged']}, untagged {counts['untagged']}, "
        + f"ambiguous {counts['ambiguous']}, collisions {counts['collisions']}, "
        + f"errors {counts['errors']}."
    )
    if not dry_run:
        click.echo(f"Playlists updated: {counts['playlists_updated']}.")
    if counts["errors"] or counts["collisions"]:
        raise SystemExit(1)
