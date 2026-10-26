"""Async CLI orchestration for one URL and its lazily resolved media items."""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING

import click

from gamdl.api import ItunesApi
from gamdl.download import Downloader, MediaDatabase, PathFormatter, PlaylistTags
from gamdl.exceptions import GamdlError
from gamdl.media import (
    MediaResolver,
    MusicVideo,
    MusicVideoCodec,
    SelectionKind,
    Song,
    SongCodec,
    UploadedVideo,
    UploadedVideoQuality,
)
from ..progress import RipProgress
from ..prompts import interactive, select_stream
from ..errors import short_error as _short_error
from ..output import banner, console as output_console, success, warning
from ..session import authenticate as _authenticate, create_drm

if TYPE_CHECKING:
    from ..options import RipOptions


class _StopRip(Exception):
    """Signal that the failed track was already counted under --fail-fast."""


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
                    "--music-video-codec cannot be combined with --music-video-codec-priority"
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


def _playlist_tags(payload: dict, playlist_id: str, index: int) -> PlaylistTags:
    resources = payload.get("data") if isinstance(payload, dict) else None
    item = resources[0] if isinstance(resources, list) and resources else {}
    attrs = item.get("attributes", {}) if isinstance(item, dict) else {}
    return PlaylistTags(
        title=attrs.get("name") or playlist_id,
        artist=attrs.get("curatorName") or "Unknown Playlist Artist",
        playlist_id=playlist_id,
        track=index + 1,
    )


async def run_rip(options: RipOptions) -> int:
    """Process discovered tracks concurrently, returning a stable CLI exit code."""
    _check_options(options)
    console = output_console()
    banner(console)
    formatter = _formatter(options)
    counts: Counter[str] = Counter()
    found = 0
    pending: set[asyncio.Task] = set()

    try:
        async with AsyncExitStack() as stack:
            api, wrapper = await _authenticate(options, stack, console=console)
            success("Authenticated")
            if not api.active_subscription:
                warning("An active Apple Music subscription was not confirmed")
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
            resolver = MediaResolver(api)
            metadata_by_playlist: dict[str, dict] = {}

            with RipProgress(
                enabled=options.progress,
                max_visible=options.max_visible_tracks,
                console=console,
            ) as progress:

                async def choose(request):
                    with progress.suspend():
                        return await select_stream(request)

                async def process(occurrence: int, media, title: str) -> None:
                    try:
                        playlist_tags = None
                        if (
                            options.save_playlist
                            and media.source is not None
                            and media.source.kind == SelectionKind.PLAYLIST_TRACKS.value
                        ):
                            # Metadata is fetched once per playlist before workers
                            # are scheduled, preserving relative M3U positions.
                            info = metadata_by_playlist.get(media.source.id)
                            if info is not None:
                                playlist_tags = _playlist_tags(
                                    info, media.source.id, media.source.index
                                )
                        if (
                            isinstance(media, MusicVideo)
                            and downloader.itunes_api is None
                            and wrapper is None
                        ):
                            # This happens before first music-video transfer;
                            # the orchestrator sets it once in the producer.
                            raise RuntimeError("iTunes metadata client not initialized")
                        kwargs = {
                            "overwrite": options.overwrite,
                            "save_cover": options.save_cover,
                            "save_lyrics": options.save_lyrics
                            and isinstance(media, Song),
                            "save_playlist": bool(playlist_tags),
                            "playlist_tags": playlist_tags,
                            "event_callback": lambda e: progress.event(
                                occurrence, title, e
                            ),
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
                                selector=choose if options.ask_song_codec else None,
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
                                selector=choose
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
                                selector=choose
                                if options.ask_uploaded_video_quality
                                else None,
                            )
                        result = await downloader.download(media, **kwargs)
                        counts[result.status.value] += 1
                        progress.finish(occurrence, title, result.status.value)
                    except click.Abort:
                        raise
                    except Exception as error:
                        message, known = _short_error(error)
                        counts["failed"] += 1
                        progress.finish(occurrence, title, "failed", message)
                        if options.debug and not known:
                            console.print_exception(show_locals=False)
                        if not options.continue_on_error:
                            raise _StopRip() from error

                stopped = False
                try:
                    async for media in resolver.from_url(options.url):
                        found += 1
                        progress.discovered(found)
                        title = f"Track {found}"
                        # Catalog/playlist iteration seeds metadata. Avoid a
                        # separate network request solely to print track titles.
                        try:
                            meta = await media.get_metadata()
                            title = meta.get("attributes", {}).get("name") or media.id
                        except GamdlError:
                            pass

                        if (
                            options.save_playlist
                            and media.source is not None
                            and media.source.kind == SelectionKind.PLAYLIST_TRACKS.value
                        ):
                            if media.source.id not in metadata_by_playlist:
                                metadata_by_playlist[
                                    media.source.id
                                ] = await api.get_playlist(
                                    media.source.id, limit_tracks=1
                                )
                        if (
                            isinstance(media, MusicVideo)
                            and downloader.itunes_api is None
                            and wrapper is None
                        ):
                            downloader.itunes_api = await stack.enter_async_context(
                                await ItunesApi.create(
                                    storefront=api.storefront, language=options.language
                                )
                            )
                        progress.add(found, title)
                        pending.add(asyncio.create_task(process(found, media, title)))
                        max_active = (
                            1
                            if any(
                                (
                                    options.ask_song_codec,
                                    options.ask_music_video_streams,
                                    options.ask_uploaded_video_quality,
                                )
                            )
                            else options.concurrent_downloads
                        )
                        if len(pending) >= max_active:
                            done, pending = await asyncio.wait(
                                pending, return_when=asyncio.FIRST_COMPLETED
                            )
                            for finished in done:
                                await finished
                except _StopRip:
                    stopped = True
                    for task in pending:
                        task.cancel()
                except click.Abort:
                    for task in pending:
                        task.cancel()
                    raise
                except Exception as error:
                    counts["failed"] += 1
                    message, known = _short_error(error)
                    console.print(f"[red]✗ Source: {message}[/red]")
                    if options.debug and not known:
                        console.print_exception(show_locals=False)
                finally:
                    progress.discovery_finished(found)
                    if pending:
                        done = await asyncio.gather(*pending, return_exceptions=True)
                        for failure in done:
                            if stopped and isinstance(failure, asyncio.CancelledError):
                                continue
                            if isinstance(failure, BaseException) and not isinstance(
                                failure, Exception
                            ):
                                raise failure

    except click.Abort:
        raise
    except Exception as error:
        message, known = _short_error(error)
        console.print(f"[red]✗ {message}[/red]")
        if options.debug and not known:
            console.print_exception(show_locals=False)
        return 1

    if found == 0 and not counts["failed"]:
        console.print("[yellow]No supported media found at this URL.[/yellow]")
        return 1
    console.print(
        f"[bold]Finished:[/bold] {counts['downloaded']} downloaded, "
        f"{counts['overwritten']} overwritten, {counts['skipped']} skipped, "
        f"{counts['failed']} failed ({found} discovered)"
    )
    return 1 if counts["failed"] else 0
