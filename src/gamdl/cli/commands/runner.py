"""Async CLI orchestration for one URL and its lazily resolved media items."""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import AsyncExitStack, aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.text import Text

from gamdl.api import ItunesApi
from gamdl.cli.tui.search import select_search_results
from gamdl.download import (
    Downloader,
    DownloadFinalizationError,
    MediaDatabase,
    PathFormatter,
)
from gamdl.download.events import DownloadStage, EventState
from gamdl.exceptions import GamdlError
from gamdl.media import (
    MediaNotFoundError,
    MediaNotStreamableError,
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
from gamdl.search import ITUNES_SEARCH_TYPES, create_search_caches

from ..errors import (
    FailureTraceback,
    capture_failure_traceback,
    print_failure_tracebacks,
)
from ..errors import (
    short_error as _short_error,
)
from ..options import TAG_NAMES
from ..output import console as output_console
from ..output_setup import resolve_output_directory
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


def _print_failures(console, failures: list[_Failure]) -> None:
    """Render errors after the live progress display has been stopped."""
    if not failures:
        return
    console.print(f"[bold red]Errors ({len(failures)}):[/bold red]")
    for failure in failures:
        suffix = "" if failure.expected else f" ({failure.exception_type})"
        console.print(Text(f"  ✗ {failure.label}: {failure.message}{suffix}"))
    print_failure_tracebacks(
        console,
        [
            FailureTraceback(failure.label, failure.traceback)
            for failure in failures
            if failure.traceback is not None
        ],
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
                capture_failure_traceback(label, error).traceback
                if not expected and not options.no_tracebacks
                else None,
            )
        )

    try:
        async with AsyncExitStack() as stack:
            api, wrapper = await _authenticate(options, stack, console=console)
            console.print("[green]✓[/green] Authenticated")
            options.output_dir = await resolve_output_directory(options.output_dir)
            formatter = _formatter(options)
            if not api.active_subscription:
                console.print(
                    "[yellow]![/yellow] An active Apple Music subscription "
                    "was not confirmed"
                )
            drm = await create_drm(options, api)
            downloader = Downloader(
                drm=drm,
                wrapper_api=wrapper,
                temp_path=options.temp_path,
                path_formatter=formatter,
                database=MediaDatabase(
                    Path(options.database_path).expanduser(),
                    root=formatter.output_dir,
                    save_metadata=options.save_metadata,
                    on_created=lambda path: click.echo(f"Created database at {path}"),
                )
                if options.database and options.database_path
                else None,
                concurrency=options.segment_concurrency,
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
                            "cover_format": options.cover_format,
                            "save_lyrics": not options.no_lyrics_file
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
                                video_extension=f".{options.music_video_container}",
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
                    except (MediaNotStreamableError, MediaNotFoundError) as error:
                        # Missing catalog playback rights are expected skips,
                        # not codec failures or retryable download errors.
                        counts["skipped"] += 1
                        progress.finish(
                            occurrence, title, "skipped", _short_error(error)[0]
                        )
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
                                    type_map = {
                                        "song": "songs",
                                        "music-video": "music-videos",
                                        "album": "albums",
                                        "playlist": "playlists",
                                        "user-playlist": "user-playlists",
                                    }
                                    chosen_type = (
                                        type_map[options.search_type]
                                        if options.search_type is not None
                                        else None
                                    )
                                    search_api = getattr(
                                        options, "search_api", "apple-music"
                                    )
                                    if search_api == "itunes" and chosen_type in {
                                        "playlists",
                                        "user-playlists",
                                    }:
                                        raise click.UsageError(
                                            "iTunes search does not support playlists"
                                        )
                                    resource_types = (
                                        (chosen_type,)
                                        if chosen_type is not None
                                        else (
                                            ITUNES_SEARCH_TYPES
                                            if search_api == "itunes"
                                            else tuple(type_map.values())
                                        )
                                    )
                                    itunes = (
                                        await stack.enter_async_context(
                                            await ItunesApi.create(
                                                storefront=api.storefront,
                                                search_only=True,
                                            )
                                        )
                                        if search_api == "itunes"
                                        else None
                                    )
                                    caches = create_search_caches(
                                        api,
                                        options.search_term,
                                        resource_types,
                                        search_api=search_api,
                                        itunes_api=itunes,
                                    )

                                    if options.feeling_lucky:
                                        assert chosen_type is not None
                                        first = await caches[chosen_type].get(0)
                                        selected = (
                                            {chosen_type: (0,)}
                                            if first and first.items
                                            else {}
                                        )
                                    else:
                                        selected = await prompt_search(
                                            select_search_results(
                                                caches,
                                                options.search_term,
                                                media_type=chosen_type,
                                            )
                                        )
                                    if not selected:
                                        raise click.ClickException(
                                            f"No search results selected for {options.search_term!r}"
                                        )
                                    for resource_type, indices in selected.items():
                                        cache = caches[resource_type]
                                        wanted = set(indices)
                                        if not wanted:
                                            continue
                                        last_index = max(wanted)
                                        async for index, metadata in cache.iter_items():
                                            if index in wanted:
                                                if resource_type == "albums":
                                                    async for (
                                                        track
                                                    ) in resolver.from_album(
                                                        metadata["id"]
                                                    ):
                                                        yield track
                                                elif resource_type in {
                                                    "playlists",
                                                    "user-playlists",
                                                }:
                                                    async for (
                                                        track
                                                    ) in resolver.from_playlist(
                                                        metadata["id"]
                                                    ):
                                                        yield track
                                                elif (
                                                    metadata.get("_search_api")
                                                    == "itunes"
                                                ):
                                                    # iTunes records are not Apple
                                                    # Music catalog metadata. Resolve
                                                    # the selected ID from Apple Music.
                                                    yield Media.from_id(
                                                        metadata["id"],
                                                        media_type=metadata["type"],
                                                        api=api,
                                                        cover_cache=resolver.cover_cache,
                                                    )
                                                else:
                                                    yield Media.from_metadata(
                                                        metadata,
                                                        api=api,
                                                        cover_cache=resolver.cover_cache,
                                                    )
                                            if index >= last_index:
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
                except Exception as error:  # noqa: BLE001 - report discovery failures
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
    except Exception as error:  # noqa: BLE001 - report session failures
        record_failure("Session", error)
        _print_failures(console, failures)
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
    _print_failures(console, failures)
    return 1 if counts["failed"] or counts["finalization_failed"] else 0
