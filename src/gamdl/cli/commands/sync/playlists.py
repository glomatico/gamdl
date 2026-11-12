"""Synchronize local M3U playlist membership with Apple Music."""

from __future__ import annotations

import asyncio
import os
from collections import Counter
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import click
import httpx
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
)
from rich.table import Table
from rich.text import Text

from gamdl.api import ItunesApi
from gamdl.download import Downloader, MediaDatabase, PathFormatter
from gamdl.media import Media, MusicVideo, MusicVideoCodec, Song, SongCodec
from gamdl.sync.playlists import (
    LocalMediaIndex,
    PlaylistPlan,
    PlaylistSnapshot,
    PlaylistSyncError,
    Track,
    plan_playlist,
    publish_playlist,
    remote_track,
    render_playlist,
)
from gamdl.transport import RequestPacer

from ...errors import (
    FailureTraceback,
    capture_failure_traceback,
    print_failure_tracebacks,
    short_error,
)
from ...output import console
from ...progress import RipProgress
from ...session import authenticate, create_drm
from ...warnings import (
    warn_alac_without_wrapper,
    warn_content_restrictions,
    warn_high_resolution_security,
)


@dataclass
class _PlaylistReport:
    name: str
    tracks: int | None = None
    added: int = 0
    removed: int = 0
    missing: int = 0
    status: str = ""
    error: str | None = None


def _playlist_name(snapshot: PlaylistSnapshot) -> str:
    """Use the actual playlist title, not its long filesystem path."""
    return next(
        (
            line.partition(":")[2]
            for line in snapshot.header
            if line.startswith("#PLAYLIST:") and line.partition(":")[2]
        ),
        snapshot.path.stem,
    )


def _describe_error(error: Exception) -> str:
    """Keep expected playlist messages; tracebacks give unexpected details."""
    name = type(error).__name__
    if isinstance(error, PlaylistSyncError):
        return f"{name}: {error}"
    message, _ = short_error(error)
    return f"{name}: {message}"


def _include_traceback(error: Exception, *, no_tracebacks: bool) -> bool:
    return not no_tracebacks and (
        not isinstance(error, PlaylistSyncError) and not short_error(error)[1]
    )


def _print_report(output, reports: list[_PlaylistReport], stats, *, dry_run: bool):
    output.print()
    output.print(
        f"[bold]{stats['checked']} playlists checked[/bold]  |  "
        f"[yellow]{stats['would_update'] if dry_run else stats['updated']} "
        f"{'need updates' if dry_run else 'updated'}[/yellow]  |  "
        f"[green]{stats['unchanged']} up to date[/green]  |  "
        f"[red]{stats['failed']} failed[/red]"
    )
    if reports:
        table = Table(title="Playlists needing attention", show_lines=False)
        table.add_column("Playlist", overflow="fold")
        table.add_column("+", justify="right", style="green")
        table.add_column("−", justify="right", style="red")
        table.add_column("Need DL", justify="right", style="yellow")
        table.add_column("Action")
        for item in reports:
            table.add_row(
                Text(item.name),
                f"+{item.added}" if item.added else "—",
                f"-{item.removed}" if item.removed else "—",
                str(item.missing) if item.missing else "—",
                item.status,
            )
        output.print(table)
    else:
        output.print("[green]All local playlists are up to date.[/green]")

    if stats["added"] or stats["removed"] or stats["missing"]:
        output.print(
            f"Detected: [green]{stats['added']} entries added[/green], "
            f"[red]{stats['removed']} removed from playlists[/red], "
            f"[yellow]{stats['missing']} initially missing locally[/yellow]."
        )
    for item in reports:
        if item.error:
            output.print(f"[red]✗[/red] {Text(item.name)}: {Text(item.error)}")
    if dry_run:
        output.print(
            "[dim]+/− = playlist entries; Need DL = tracks missing locally. "
            "Different IDs can represent alternate releases.[/dim]"
        )
        output.print(
            "[dim]Dry run: no downloads or playlist edits. Removed entries "
            "never delete local media.[/dim]"
        )


def _formatter(root: Path, output) -> PathFormatter:
    return PathFormatter(
        root,
        album_folder_template=output.album_folder_template,
        compilation_folder_template=output.compilation_folder_template,
        no_album_folder_template=output.no_album_folder_template,
        playlist_folder_template=output.playlist_folder_template,
        single_disc_file_template=output.single_disc_file_template,
        multi_disc_file_template=output.multi_disc_file_template,
        no_album_file_template=output.no_album_file_template,
        playlist_file_template=output.playlist_file_template,
        truncate=output.truncate,
    )


def _auth_options(settings) -> SimpleNamespace:
    return SimpleNamespace(
        auth_mode=settings.auth.mode,
        cookies_path=settings.auth.cookies_path,
        wrapper_url=settings.auth.wrapper_url,
        media_user_token=settings.auth.media_user_token,
        storefront=settings.auth.storefront,
        language=settings.auth.language,
        drm_backend=settings.auth.drm_backend,
        device_path=settings.auth.device_path,
        wrapper_decrypt_host=settings.auth.wrapper_decrypt_host,
        wrapper_decrypt_port=settings.auth.wrapper_decrypt_port,
    )


def _select_playlists(
    root: Path, selected: tuple[Path, ...], limit: int | None
) -> list[Path]:
    if selected:
        paths = []
        for value in selected:
            path = Path(os.path.abspath(root / value))
            if (
                not path.resolve().is_relative_to(root.resolve())
                or path.suffix.lower() != ".m3u8"
            ):
                raise click.BadParameter(
                    "Playlist must be a .m3u8 file within the chosen library",
                    param_hint="--playlist",
                )
            if path not in paths:
                paths.append(path)
    else:
        paths = sorted(root.rglob("*.m3u8"))
    return paths[:limit] if limit is not None else paths


def _show_membership_changes(
    plan: PlaylistPlan, index: LocalMediaIndex, output
) -> None:
    """Show the exact additions/removals before any playlist is rewritten."""
    old_keys = [index.identity(entry.path) for entry in plan.snapshot.entries]
    old = Counter(old_keys)
    new = Counter(track.key for track in plan.tracks)
    added = new - old
    removed = old - new
    shown = 0
    for track in plan.tracks:
        if added[track.key] <= 0:
            continue
        added[track.key] -= 1
        title = (track.resource.get("attributes") or {}).get("name") or track.media_id
        if shown < 8:
            output.print(Text(f"    + {title} ({track.media_id})"))
        shown += 1
    if shown > 8:
        output.print(f"    + … {shown - 8} more additions")
    shown = 0
    for entry, key in zip(plan.snapshot.entries, old_keys, strict=True):
        if removed[key] <= 0:
            continue
        removed[key] -= 1
        if shown < 8:
            output.print(Text(f"    - {entry.path.stem} ({key[1]})"))
        shown += 1
    if shown > 8:
        output.print(f"    - … {shown - 8} more removals")
    if plan.reordered and plan.added == 0 and plan.removed == 0:
        output.print("    ↕ Track order changed")


def _download_options(settings, media: Media) -> dict:
    """Use the same codec priorities and sidecars as a normal rip."""
    kwargs = {
        "overwrite": False,
        "tag": not settings.output.no_tag,
        "exclude_tags": settings.output.exclude_tags,
        "cover_format": settings.output.cover_format,
        "save_cover": settings.output.save_cover,
        "save_playlist": False,
    }
    if isinstance(media, Song):
        kwargs.update(
            codec=SongCodec(settings.download.song_codec)
            if settings.download.song_codec
            else None,
            codec_priority=tuple(
                SongCodec(value) for value in settings.download.song_codec_priority
            )
            if not settings.download.song_codec
            else None,
            save_lyrics=not settings.output.no_lyrics_file,
            lyrics_file_format=settings.output.lyrics_file_format,
            embedded_lyrics_format=settings.output.embedded_lyrics_format,
        )
    elif isinstance(media, MusicVideo):
        kwargs.update(
            video_extension=f".{settings.output.music_video_container}",
            video_codec=MusicVideoCodec(settings.download.music_video_codec)
            if settings.download.music_video_codec
            else None,
            codec_priority=tuple(
                MusicVideoCodec(value)
                for value in settings.download.music_video_codec_priority
            )
            if not settings.download.music_video_codec
            else None,
            max_height=settings.download.max_height,
        )
    return kwargs


async def _download_missing(
    *,
    settings,
    api,
    downloader: Downloader,
    index: LocalMediaIndex,
    missing: dict[tuple[str, str], Track],
    stats: Counter[str],
    output,
    diagnostics: list[FailureTraceback],
    no_tracebacks: bool,
) -> dict[tuple[str, str], str]:
    """Download unique missing IDs concurrently, keeping per-track failures isolated."""
    total = len(missing)
    workers = min(settings.download.concurrent_downloads, total)
    output.print(
        f"Discovered {total} unique missing track{'s' if total != 1 else ''}. "
        f"Downloading with {workers} concurrent track{'s' if workers != 1 else ''} "
        f"(segment concurrency: {settings.download.segment_concurrency})."
    )
    queue: asyncio.Queue[tuple[int, Track]] = asyncio.Queue()
    for occurrence, track in enumerate(missing.values(), 1):
        queue.put_nowait((occurrence, track))
    failures: dict[tuple[str, str], str] = {}
    with RipProgress(
        enabled=not settings.ui.no_progress,
        max_visible=settings.ui.max_visible_tracks,
        console=output,
    ) as progress:
        progress.start_batch(total)

        async def worker() -> None:
            while True:
                try:
                    occurrence, track = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                title = (track.resource.get("attributes") or {}).get(
                    "name"
                ) or track.media_id
                progress.add(
                    occurrence,
                    title,
                    tracks=("audio", "video")
                    if track.kind == "music-videos"
                    else ("audio",),
                )
                try:
                    media = Media.from_metadata(track.resource, api=api)
                    result = await downloader.download(
                        media,
                        **_download_options(settings, media),
                        event_callback=lambda event, number=occurrence, name=title: (
                            progress.event(number, name, event)
                        ),
                    )
                    if result.status.value == "skipped":
                        # Filename conflicts aren't proof that this ID exists.
                        identity = await asyncio.to_thread(
                            _read_downloaded_id, result.path
                        )
                        if identity != track.key:
                            raise PlaylistSyncError(
                                "Existing output filename belongs to a different "
                                f"track: {result.path}"
                            )
                    index.add(track.kind, track.media_id, result.path)
                    if index.find(track.key) is None:
                        raise PlaylistSyncError(
                            f"Download did not produce a local file: {track.media_id}"
                        )
                    stats[result.status.value] += 1
                    progress.finish(
                        occurrence, title, result.status.value, path=result.path
                    )
                except Exception as error:  # noqa: BLE001 - one failed track cannot stop others
                    failures[track.key] = _describe_error(error)
                    progress.finish(occurrence, title, "failed", failures[track.key])
                    if _include_traceback(error, no_tracebacks=no_tracebacks):
                        diagnostics.append(
                            capture_failure_traceback(
                                f"Track {title} ({track.media_id})", error
                            )
                        )
                finally:
                    queue.task_done()

        await asyncio.gather(*(worker() for _ in range(workers)))
    output.print(
        f"Downloads finished: {stats['downloaded']} downloaded, "
        f"{stats['skipped']} reused, {len(failures)} failed."
    )
    for key, reason in failures.items():
        output.print(f"[red]✗[/red] {key[0]} {key[1]}: {Text(reason)}")
    return failures


async def _run(
    settings,
    *,
    root: Path,
    selected: tuple[Path, ...],
    limit: int | None,
    dry_run: bool,
    no_download: bool,
    yes: bool,
    verbose: bool = False,
    no_tracebacks: bool | None = None,
) -> int:
    output = console()
    suppress_tracebacks = (
        settings.ui.no_tracebacks if no_tracebacks is None else no_tracebacks
    )
    paths = _select_playlists(root, selected, limit)
    if not paths:
        output.print("No local .m3u8 playlists found.")
        return 0

    db_path = Path(settings.database.path).expanduser()
    with output.status("Indexing local media IDs..."):
        index = await asyncio.to_thread(
            LocalMediaIndex, root, db_path if db_path.is_file() else None
        )
    if settings.ui.no_progress:
        output.print(f"Checking {len(paths)} playlists against Apple Music...")
    stats: Counter[str] = Counter()
    reports: list[_PlaylistReport] = []
    diagnostics: list[FailureTraceback] = []
    ready: list[tuple[PlaylistPlan, _PlaylistReport]] = []
    missing: dict[tuple[str, str], Track] = {}
    auth_options = _auth_options(settings)
    async with AsyncExitStack() as stack:
        api, wrapper = await authenticate(auth_options, stack, console=output)
        warn_content_restrictions(api, output)
        checking = stack.enter_context(
            Progress(
                TextColumn("Checking Apple Music playlists"),
                BarColumn(bar_width=24),
                MofNCompleteColumn(),
                TaskProgressColumn(),
                console=output,
                transient=True,
                disable=settings.ui.no_progress,
            )
        )
        check_task = checking.add_task("Checking", total=len(paths))
        for number, path in enumerate(paths, 1):
            stats["checked"] += 1
            report: _PlaylistReport | None = None
            try:
                snapshot = PlaylistSnapshot.read(path, root)
                resource_type = snapshot.playlist_id.startswith("p.")
                tracks = tuple(
                    [
                        remote_track(resource)
                        async for resource in api.iter_playlist_tracks(
                            snapshot.playlist_id, is_library=resource_type
                        )
                    ]
                )
                plan: PlaylistPlan = plan_playlist(snapshot, tracks, index)
                needs_update = (
                    bool(plan.missing)
                    or render_playlist(plan, index) != snapshot.original
                )
                if needs_update:
                    report = _PlaylistReport(
                        name=_playlist_name(snapshot),
                        tracks=len(tracks),
                        added=plan.added,
                        removed=plan.removed,
                        missing=len(plan.missing),
                    )
                    reports.append(report)
                elif verbose:
                    output.print(
                        f"[green]✓[/green] {_playlist_name(snapshot)}: up to date ({len(tracks)} tracks)"
                    )
                if verbose and (plan.added or plan.removed or plan.reordered):
                    output.print(
                        f"{_playlist_name(snapshot)}: "
                        f"+{plan.added}, -{plan.removed}, "
                        f"{len(plan.missing)} missing locally"
                    )
                    _show_membership_changes(plan, index, output)
                stats["added"] += plan.added
                stats["removed"] += plan.removed
                stats["missing"] += len(plan.missing)
                if dry_run:
                    if needs_update:
                        stats["would_update"] += 1
                        assert report is not None
                        report.status = (
                            "[yellow]Review[/yellow]"
                            if plan.removed > max(10, len(snapshot.entries) // 2)
                            else "Would update"
                        )
                    else:
                        stats["unchanged"] += 1
                    continue
                if not needs_update:
                    stats["unchanged"] += 1
                    continue
                assert report is not None
                if plan.missing and no_download:
                    raise PlaylistSyncError(
                        f"{len(plan.missing)} remote tracks missing locally; "
                        "--no-download preserves this playlist unchanged"
                    )
                if plan.removed > max(10, len(snapshot.entries) // 2) and not yes:
                    raise PlaylistSyncError(
                        "Large membership removal requires --yes; "
                        "review with --dry-run first"
                    )
                ready.append((plan, report))
                for track in plan.missing:
                    missing.setdefault(track.key, track)
            except Exception as error:  # noqa: BLE001 - isolate each playlist
                stats["failed"] += 1
                if report is None:
                    report = _PlaylistReport(name=path.stem)
                    reports.append(report)
                report.status = "[red]Failed[/red]"
                report.error = _describe_error(error)
                if _include_traceback(error, no_tracebacks=suppress_tracebacks):
                    diagnostics.append(
                        capture_failure_traceback(f"Playlist {path.name}", error)
                    )
            finally:
                checking.advance(check_task)
            if number < len(paths):
                await asyncio.sleep(0.4)  # Avoid bursts on catalog endpoints.
        checking.stop()
        if not dry_run:
            failures: dict[tuple[str, str], str] = {}
            if missing:
                if any(track.kind == "songs" for track in missing.values()):
                    warn_alac_without_wrapper(
                        wrapper,
                        output,
                        codec=settings.download.song_codec,
                        codec_priority=settings.download.song_codec_priority,
                    )
                # Finish ALL discovery first: a track can appear in multiple
                # playlists, but should be downloaded only once. Concurrency
                # here is per media item, not the stream-part concurrency.
                drm = await create_drm(auth_options, api)
                if any(track.kind == "music-videos" for track in missing.values()):
                    await warn_high_resolution_security(
                        drm, output, max_height=settings.download.max_height
                    )
                itunes = await stack.enter_async_context(
                    await ItunesApi.create(
                        storefront=api.storefront,
                        language=settings.auth.language,
                        pacer=RequestPacer(interval=0.35),
                    )
                )
                cdn = await stack.enter_async_context(httpx.AsyncClient(timeout=30))
                downloader = Downloader(
                    drm=drm,
                    wrapper_api=wrapper,
                    itunes_api=itunes,
                    client=cdn,
                    temp_path=settings.output.temp_path,
                    path_formatter=_formatter(root, settings.output),
                    database=MediaDatabase(
                        db_path,
                        root=root,
                        save_metadata=settings.database.save_metadata,
                    )
                    if not settings.database.disabled
                    else None,
                    concurrency=settings.download.segment_concurrency,
                )
                failures = await _download_missing(
                    settings=settings,
                    api=api,
                    downloader=downloader,
                    index=index,
                    missing=missing,
                    stats=stats,
                    output=output,
                    diagnostics=diagnostics,
                    no_tracebacks=suppress_tracebacks,
                )
            for plan, report in ready:
                try:
                    failed = [
                        track for track in plan.missing if index.find(track.key) is None
                    ]
                    if failed:
                        details = "; ".join(
                            f"{track.media_id}: {failures.get(track.key, 'not downloaded')}"
                            for track in failed[:3]
                        )
                        if len(failed) > 3:
                            details += f"; and {len(failed) - 3} more"
                        raise PlaylistSyncError(
                            f"{len(failed)} missing track(s); playlist unchanged. {details}"
                        )
                    if publish_playlist(plan, index):
                        stats["updated"] += 1
                        report.status = "[green]Updated[/green]"
                        output.print(f"[green]✓[/green] {report.name} synchronized")
                    else:
                        stats["unchanged"] += 1
                        report.status = "Unchanged"
                except Exception as error:  # noqa: BLE001 - per-playlist atomicity
                    stats["failed"] += 1
                    report.status = "[red]Failed[/red]"
                    report.error = _describe_error(error)
                    if _include_traceback(error, no_tracebacks=suppress_tracebacks):
                        diagnostics.append(
                            capture_failure_traceback(
                                f"Playlist {plan.snapshot.path.name}",
                                error,
                            )
                        )
    _print_report(output, reports, stats, dry_run=dry_run)
    if not dry_run and stats["downloaded"]:
        output.print(
            f"[green]{stats['downloaded']} new media files downloaded.[/green]"
        )
    print_failure_tracebacks(output, diagnostics)
    return int(stats["failed"] > 0)


def _read_downloaded_id(path: Path) -> tuple[str, str] | None:
    from gamdl import _native

    try:
        fields = (
            _native.read_mp3_organize_tags(str(path))
            if path.suffix.lower() == ".mp3"
            else _native.read_mp4_organize_tags(str(path))
        )
    except (OSError, ValueError, TypeError):
        return None
    media_id = fields.get("title_id")
    if media_id is None:
        return None
    kind = (
        "music-videos"
        if path.suffix.lower() == ".m4v" or fields.get("media_type") == 6
        else "songs"
    )
    return kind, str(media_id)


@click.command("playlists")
@click.option(
    "--directory",
    type=click.Path(path_type=Path, file_okay=False),
    help="Library root (default: output.directory)",
)
@click.option(
    "--playlist",
    "selected",
    multiple=True,
    type=click.Path(path_type=Path, dir_okay=False),
    help="Synchronize a specific .m3u8 (repeatable; defaults to all)",
)
@click.option("--dry-run", is_flag=True, help="Compare without writing or downloading")
@click.option(
    "--no-download",
    is_flag=True,
    help="Never download; leave a playlist unchanged if required media is absent",
)
@click.option("--yes", is_flag=True, help="Allow large playlist membership removals")
@click.option("--limit", type=click.IntRange(min=1), help="Process at most N playlists")
@click.option(
    "--verbose", is_flag=True, help="Show every playlist and individual track changes"
)
@click.option(
    "--no-tracebacks",
    is_flag=True,
    default=None,
    help="Suppress tracebacks for unexpected errors",
)
@click.pass_context
def playlists(
    ctx, directory, selected, dry_run, no_download, yes, limit, verbose, no_tracebacks
) -> None:
    """Match existing M3U playlists to Apple Music without deleting local media.

    Uses the embedded playlist ID and local catalog IDs, keeps remote ordering
    and duplicate entries, and publishes each complete playlist atomically.
    """
    settings = ctx.find_root().obj["settings"]
    if directory is None and settings.output.directory is None:
        from gamdl.cli.output_setup import resolve_output_directory

        root = Path(asyncio.run(resolve_output_directory(None)))
    else:
        root = (directory or Path(settings.output.directory)).expanduser().absolute()
    if not root.is_dir():
        raise click.ClickException(f"Library directory does not exist: {root}")
    try:
        code = asyncio.run(
            _run(
                settings,
                root=root,
                selected=selected,
                limit=limit,
                dry_run=dry_run,
                no_download=no_download,
                yes=yes,
                verbose=verbose,
                no_tracebacks=no_tracebacks,
            )
        )
    except KeyboardInterrupt:
        raise click.Abort() from None
    if code:
        raise SystemExit(code)
