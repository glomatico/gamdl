"""Async CLI orchestration for one URL and its lazily resolved media items."""

from __future__ import annotations

import asyncio
import traceback
from collections import Counter
from contextlib import AsyncExitStack, aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.text import Text

from gamdl.api import ItunesApi
from gamdl.cli.tui.components import MenuChoice
from gamdl.cli.tui.search import select_search_results
from gamdl.cli.tui.selector import select_one
from gamdl.download import (
    Downloader,
    DownloadFinalizationError,
    MediaDatabase,
    PathFormatter,
)
from gamdl.download.events import DownloadStage, EventState
from gamdl.exceptions import GamdlError
from gamdl.media import (
    MediaResolver,
    MusicVideo,
    MusicVideoCodec,
    Song,
    SongCodec,
    UploadedVideo,
    UploadedVideoQuality,
)
from gamdl.media.base import Media
from gamdl.media.constants import (
    ARTIST_AUTO_SELECT_NAMES,
    DEFAULT_AUTO_SELECT,
    LIBRARY_AUTO_SELECT_NAMES,
)
from gamdl.pagination import MediaPage, PageCache

from ..errors import short_error as _short_error
from ..options import TAG_NAMES
from ..output import console as output_console
from ..progress import RipProgress
from ..prompts import interactive, select_media, select_stream
from ..session import authenticate as _authenticate
from ..session import create_drm
from ..terminal import TerminalController

if TYPE_CHECKING:
    from ..options import RipOptions


class _StopRip(Exception):
    """Signal that the failed track was already counted under --fail-fast."""


@dataclass(frozen=True)
class _Failure:
    label: str
    message: str
    exception_type: str
    expected: bool
    traceback: str | None


def _print_failures(console, failures: list[_Failure], *, debug: bool) -> None:
    """Render errors after the live progress display has been stopped."""
    if not failures:
        return
    console.print(f"[bold red]Errors ({len(failures)}):[/bold red]")
    for failure in failures:
        suffix = "" if failure.expected else f" ({failure.exception_type})"
        console.print(Text(f"  ✗ {failure.label}: {failure.message}{suffix}"))
        if debug and failure.traceback is not None:
            console.print(Text(failure.traceback))
    has_unexpected = any(not failure.expected for failure in failures)
    if has_unexpected and not debug:
        console.print("[dim]Run with --debug for tracebacks.[/dim]")
    elif any(failure.traceback is not None for failure in failures):
        console.print(
            "[yellow]Debug tracebacks may contain signed URLs or account details; "
            "review before sharing.[/yellow]"
        )


def _check_options(options: RipOptions) -> None:
    if (
        any(
            (
                options.ask_song_codec,
                options.ask_music_video_streams,
                options.ask_uploaded_video_quality,
            )
        )
        and not interactive()
    ):
        raise click.UsageError(
            "Interactive codec/quality selection requires a terminal"
        )
    ctx = click.get_current_context(silent=True)
    from click.core import ParameterSource

    def explicitly_set(key: str) -> bool:
        return ctx is not None and ctx.get_parameter_source(key) in {
            ParameterSource.COMMANDLINE,
            ParameterSource.ENVIRONMENT,
        }

    # An interactive flag overrides YAML codec preferences, but conflicting
    # explicit CLI options remain an error.
    if options.ask_song_codec and options.song_codec and explicitly_set("song_codec"):
        raise click.UsageError("--ask-song-codec conflicts with --song-codec")
    if (
        options.ask_music_video_streams
        and options.music_video_codec
        and explicitly_set("music_video_codec")
    ):
        raise click.UsageError(
            "--ask-music-video-streams conflicts with --music-video-codec"
        )
    if (
        options.ask_uploaded_video_quality
        and options.uploaded_video_quality
        and explicitly_set("uploaded_video_quality")
    ):
        raise click.UsageError(
            "--ask-uploaded-video-quality conflicts with --uploaded-video-quality"
        )
    if options.song_codec and options.song_codec_priority:
        # The default YAML codec priority is overridden by an exact codec;
        # it must not be treated as a mutually exclusive explicit option.
        ctx = click.get_current_context(silent=True)
        if ctx is not None:
            from click.core import ParameterSource

            if ctx.get_parameter_source("song_codec") in {
                ParameterSource.COMMANDLINE,
                ParameterSource.ENVIRONMENT,
            } and ctx.get_parameter_source("song_codec_priority") in {
                ParameterSource.COMMANDLINE,
                ParameterSource.ENVIRONMENT,
            }:
                raise click.UsageError(
                    "--song-codec cannot be combined with --song-codec-priority"
                )
    if options.music_video_codec and options.music_video_codec_priority:
        ctx = click.get_current_context(silent=True)
        if ctx is not None:
            from click.core import ParameterSource

            if ctx.get_parameter_source("music_video_codec") in {
                ParameterSource.COMMANDLINE,
                ParameterSource.ENVIRONMENT,
            } and ctx.get_parameter_source("music_video_codec_priority") in {
                ParameterSource.COMMANDLINE,
                ParameterSource.ENVIRONMENT,
            }:
                raise click.UsageError(
                    "--music-video-codec cannot be combined with "
                    "--music-video-codec-priority"
                )


def _formatter(options: RipOptions) -> PathFormatter:
    return PathFormatter(
        Path(options.output_dir).expanduser(),
        album_folder_template=options.album_folder_template,
        compilation_folder_template=options.compilation_folder_template,
        no_album_folder_template=options.no_album_folder_template,
        playlist_folder_template=options.playlist_folder_template,
        single_disc_file_template=options.single_disc_file_template,
        multi_disc_file_template=options.multi_disc_file_template,
        no_album_file_template=options.no_album_file_template,
        playlist_file_template=options.playlist_file_template,
        truncate=options.truncate,
    )


async def run_rip(options: RipOptions) -> int:
    """Process discovered tracks concurrently, returning a stable CLI exit code."""
    _check_options(options)
    if not options.urls:
        raise click.UsageError("Provide at least one Apple Music URL")
    artist_values = [
        v.strip() for group in options.artist_auto_select for v in group.split(",")
    ]
    library_values = [
        v.strip()
        for group in getattr(options, "library_auto_select", ())
        for v in group.split(",")
    ]
    if any(v not in ARTIST_AUTO_SELECT_NAMES and v != "all" for v in artist_values):
        raise click.UsageError("Invalid --artist-auto-select option")
    if any(v not in LIBRARY_AUTO_SELECT_NAMES and v != "all" for v in library_values):
        raise click.UsageError("Invalid --library-auto-select option")
    excluded = tuple(
        name.strip() for group in options.exclude_tags for name in group.split(",")
    )
    if any(name not in TAG_NAMES for name in excluded):
        raise click.UsageError(
            "Invalid --exclude-tags field (valid: " + ", ".join(TAG_NAMES) + ")"
        )
    console = output_console()
    formatter = _formatter(options)
    counts: Counter[str] = Counter()
    found = 0
    pending: set[asyncio.Task] = set()
    failures: list[_Failure] = []

    def record_failure(label: str, error: Exception) -> None:
        message, expected = _short_error(error)
        failures.append(
            _Failure(
                label,
                message,
                type(error).__name__,
                expected,
                "".join(traceback.format_exception(error))
                if options.debug and not expected
                else None,
            )
        )

    try:
        async with AsyncExitStack() as stack:
            api, wrapper = await _authenticate(options, stack, console=console)
            console.print("[green]✓[/green] Authenticated")
            if not api.active_subscription:
                console.print(
                    "[yellow]![/yellow] An active Apple Music subscription "
                    "was not confirmed"
                )
            drm = await create_drm(options, api)
            downloader = Downloader(
                drm=drm,
                wrapper_api=wrapper,
                path_formatter=formatter,
                database=MediaDatabase(Path(options.database_path).expanduser())
                if options.database and options.database_path
                else None,
                concurrency=options.concurrency,
            )
            with RipProgress(
                enabled=not options.no_progress,
                max_visible=options.max_visible_tracks,
                console=console,
            ) as progress:
                terminal = TerminalController(progress)
                stop_requested: asyncio.Future[BaseException] = (
                    asyncio.get_running_loop().create_future()
                )

                def request_stop(error: BaseException) -> None:
                    # Signal before the worker yields, so queued selectors
                    # cannot open another menu after its lease is released.
                    if not stop_requested.done():
                        stop_requested.set_result(error)

                def check_stopped() -> None:
                    if stop_requested.done():
                        raise stop_requested.result()

                async def choose(request, *, media_title: str):
                    check_stopped()
                    return await terminal.prompt(
                        select_stream(request, media_title=media_title)
                    )

                async def choose_media(request):
                    # Previously selected tracks may keep downloading. Only
                    # competing menus are serialized, not their transfers.
                    check_stopped()
                    async with terminal.session():
                        check_stopped()
                        try:
                            return await terminal.prompt(select_media(request))
                        except click.Abort as error:
                            request_stop(error)
                            raise

                resolver = MediaResolver(
                    api,
                    auto_select=[
                        *DEFAULT_AUTO_SELECT,
                        *(
                            ARTIST_AUTO_SELECT_NAMES[name]
                            for name in artist_values
                            if name != "all"
                        ),
                        *(
                            ARTIST_AUTO_SELECT_NAMES[name]
                            for name in ("all-albums", "music-videos")
                            if "all" in artist_values
                        ),
                        *(
                            LIBRARY_AUTO_SELECT_NAMES[name]
                            for name in library_values
                            if name != "all"
                        ),
                        *(
                            LIBRARY_AUTO_SELECT_NAMES.values()
                            if "all" in library_values
                            else ()
                        ),
                    ],
                    select=options.select,
                    library_auto_select=bool(library_values),
                    selector=choose_media,
                )

                async def process(occurrence: int, media, title: str) -> None:
                    try:
                        lease = None
                        check_stopped()

                        async def choose_for_track(request):
                            nonlocal lease
                            check_stopped()
                            if lease is None:
                                lease = await terminal.acquire()
                            return await choose(request, media_title=title)

                        def on_event(event):
                            progress.event(occurrence, title, event)
                            if (
                                lease is not None
                                and event.stage == DownloadStage.FETCHING_STREAMS
                                and event.state
                                in {EventState.COMPLETED, EventState.FAILED}
                            ):
                                lease.release()

                        kwargs = {
                            "overwrite": options.overwrite,
                            "tag": not options.no_tag,
                            "exclude_tags": excluded,
                            "save_cover": options.save_cover,
                            "save_lyrics": not options.no_lyrics
                            and isinstance(media, Song),
                            "save_playlist": options.save_playlist
                            and media.source is not None
                            and media.source.playlist is not None,
                            "event_callback": on_event,
                        }
                        if isinstance(media, Song):
                            kwargs.update(
                                codec=SongCodec(options.song_codec)
                                if options.song_codec and not options.ask_song_codec
                                else None,
                                codec_priority=None
                                if options.song_codec or options.ask_song_codec
                                else [SongCodec(c) for c in options.song_codec_priority]
                                or None,
                                ask=options.ask_song_codec,
                                selector=choose_for_track
                                if options.ask_song_codec
                                else None,
                            )
                        elif isinstance(media, MusicVideo):
                            kwargs.update(
                                video_codec=MusicVideoCodec(options.music_video_codec)
                                if options.music_video_codec
                                and not options.ask_music_video_streams
                                else None,
                                codec_priority=None
                                if options.music_video_codec
                                or options.ask_music_video_streams
                                else [
                                    MusicVideoCodec(c)
                                    for c in options.music_video_codec_priority
                                ]
                                or None,
                                max_height=options.max_height,
                                ask=options.ask_music_video_streams,
                                selector=choose_for_track
                                if options.ask_music_video_streams
                                else None,
                            )
                        elif isinstance(media, UploadedVideo):
                            kwargs.update(
                                quality=UploadedVideoQuality(
                                    options.uploaded_video_quality
                                )
                                if options.uploaded_video_quality
                                and not options.ask_uploaded_video_quality
                                else None,
                                ask=options.ask_uploaded_video_quality,
                                selector=choose_for_track
                                if options.ask_uploaded_video_quality
                                else None,
                            )
                        # The first prompt acquires the lease, allowing tags,
                        # path checks and stream discovery to run concurrently.
                        # Keep it through subsequent menus for this track;
                        # the stream-stage event releases it before transfer.
                        try:
                            result = await downloader.download(media, **kwargs)
                        finally:
                            if lease is not None:
                                lease.release()
                        counts[result.status.value] += 1
                        progress.finish(
                            occurrence, title, result.status.value, path=result.path
                        )
                    except _StopRip:
                        # Another worker already recorded the fatal failure.
                        raise
                    except click.Abort as error:
                        request_stop(error)
                        raise
                    except DownloadFinalizationError as error:
                        record_failure(title, error)
                        if error.partial_result is not None:
                            counts[error.partial_result.status.value] += 1
                            counts["finalization_failed"] += 1
                            progress.finish(
                                occurrence,
                                title,
                                "partial",
                                _short_error(error)[0],
                                path=error.partial_result.path,
                            )
                        else:
                            counts["failed"] += 1
                            progress.finish(
                                occurrence, title, "failed", _short_error(error)[0]
                            )
                        if options.fail_fast:
                            stop = _StopRip()
                            request_stop(stop)
                            raise stop from error
                    except Exception as error:
                        record_failure(title, error)
                        message, _ = _short_error(error)
                        counts["failed"] += 1
                        progress.finish(occurrence, title, "failed", message)
                        if options.fail_fast:
                            stop = _StopRip()
                            request_stop(stop)
                            raise stop from error

                async def wait_for_worker() -> None:
                    nonlocal pending
                    done, pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED
                    )
                    # Retrieve every completed outcome even if one stops the
                    # run, so simultaneous failures cannot become orphan tasks.
                    results = await asyncio.gather(*done, return_exceptions=True)
                    for result in results:
                        if isinstance(result, click.Abort):
                            raise result
                    for result in results:
                        if isinstance(result, BaseException):
                            raise result

                async def discover() -> None:
                    nonlocal found
                    for url in options.urls:
                        check_stopped()
                        try:
                            if hasattr(options, "search_term"):

                                async def prompt_search(operation):
                                    async with terminal.session():
                                        return await terminal.prompt(operation)

                                async def search_source():
                                    while True:
                                        kind = options.search_type
                                        if kind is None:
                                            kind = (
                                                "song",
                                                "music-video",
                                                "uploaded-video",
                                            )[
                                                await prompt_search(
                                                    select_one(
                                                        "Search media type",
                                                        [
                                                            MenuChoice("Songs", 0),
                                                            MenuChoice(
                                                                "Music videos", 1
                                                            ),
                                                            MenuChoice(
                                                                "Uploaded videos", 2
                                                            ),
                                                        ],
                                                    )
                                                )
                                            ]
                                        resource_type = {
                                            "song": "songs",
                                            "music-video": "music-videos",
                                            "uploaded-video": "uploaded-videos",
                                        }[kind]

                                        async def load(cursor):
                                            response = (
                                                await api.get_extended_api_data(cursor)
                                                if cursor is not None
                                                else await api.get_search_results(
                                                    options.search_term,
                                                    types=resource_type,
                                                    limit=50,
                                                )
                                            )
                                            group = response.get("results", {}).get(
                                                resource_type, {}
                                            )
                                            data = group.get("data", [])
                                            return MediaPage(
                                                tuple(data), group.get("next")
                                            )

                                        cache = PageCache(load)
                                        if options.feeling_lucky:
                                            first = await cache.get(0)
                                            indices = (
                                                [0] if first and first.items else []
                                            )
                                        else:
                                            indices = await prompt_search(
                                                select_search_results(
                                                    cache, options.search_term
                                                )
                                            )
                                        if indices == "back":
                                            if options.search_type is not None:
                                                raise click.Abort()
                                            continue
                                        break
                                    wanted = set(indices)
                                    if not wanted:
                                        raise click.ClickException(
                                            f"No {kind} search results for {options.search_term!r}"
                                        )
                                    async for index, metadata in cache.iter_items():
                                        if index in wanted:
                                            yield Media.from_metadata(
                                                metadata,
                                                api=api,
                                                cover_cache=resolver.cover_cache,
                                            )
                                        if wanted and index >= max(wanted):
                                            break

                                source_iterator = search_source()
                            else:
                                source_iterator = resolver.from_url(url)
                            async with aclosing(source_iterator) as source:
                                async for media in source:
                                    check_stopped()
                                    found += 1
                                    progress.discovered(found)
                                    title = f"Track {found}"
                                    # Catalog/playlist iterators supply metadata;
                                    # avoid an extra request just to print titles.
                                    try:
                                        meta = await media.get_metadata()
                                        title = (
                                            meta.get("attributes", {}).get("name")
                                            or media.id
                                        )
                                    except GamdlError:
                                        pass

                                    if (
                                        isinstance(media, MusicVideo)
                                        and downloader.itunes_api is None
                                    ):
                                        from gamdl.api import GamdlApiError

                                        try:
                                            downloader.itunes_api = (
                                                await stack.enter_async_context(
                                                    await ItunesApi.create(
                                                        storefront=api.storefront,
                                                        language=options.language,
                                                    )
                                                )
                                            )
                                        except GamdlApiError:
                                            pass
                                    check_stopped()
                                    progress.add(
                                        found,
                                        title,
                                        tracks=("audio", "video")
                                        if isinstance(media, MusicVideo)
                                        else ("video",)
                                        if isinstance(media, UploadedVideo)
                                        else ("audio",),
                                    )
                                    pending.add(
                                        asyncio.create_task(
                                            process(found, media, title)
                                        )
                                    )
                                    max_active = options.concurrent_downloads
                                    if len(pending) >= max_active:
                                        await wait_for_worker()
                        except (_StopRip, click.Abort) as error:
                            request_stop(error)
                            raise
                        except Exception as error:
                            record_failure("Source", error)
                            counts["failed"] += 1
                            if options.fail_fast:
                                stop = _StopRip()
                                request_stop(stop)
                                raise stop from error
                    progress.discovery_finished(found)
                    while pending:
                        await wait_for_worker()

                discovery = asyncio.create_task(discover())
                try:
                    # A worker may abort while discovery awaits a page,
                    # metadata, or a collection menu. Observe that failure
                    # immediately instead of waiting to fill the worker pool.
                    await asyncio.wait(
                        (discovery, stop_requested),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    check_stopped()
                    await discovery
                except _StopRip:
                    pass
                except click.Abort:
                    raise
                except Exception as error:
                    record_failure("Source", error)
                    counts["failed"] += 1
                finally:
                    # Abort, fail-fast and external cancellation must stop
                    # downloads and menus before their terminal UI is closed.
                    discovery.cancel()
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(discovery, *pending, return_exceptions=True)
                    stop_requested.cancel()
                    pending.clear()
                    progress.discovery_finished(found)

    except click.Abort:
        raise
    except Exception as error:
        record_failure("Session", error)
        _print_failures(console, failures, debug=options.debug)
        return 1

    if found == 0 and not counts["failed"]:
        console.print("[yellow]No supported media found at this URL.[/yellow]")
        return 1
    console.print(
        f"[bold]Finished:[/bold] {counts['downloaded']} downloaded, "
        f"{counts['overwritten']} overwritten, {counts['skipped']} skipped, "
        f"{counts['failed']} failed, {counts['finalization_failed']} "
        f"finalization failures ({found} discovered)"
    )
    _print_failures(console, failures, debug=options.debug)
    return 1 if counts["failed"] or counts["finalization_failed"] else 0
