"""Single-item downloads built from the public media, stream and mux layers.

No global login or terminal UI: the caller provides an authenticated media
object, any DRM/wrapper services, output options, and optional UI callbacks.
"""

from __future__ import annotations

import asyncio
import os
import warnings
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

import httpx

from gamdl.api import ItunesApi
from gamdl.api.wrapper import WrapperApi
from gamdl.drm import Drm
from gamdl.media import (
    AudioStream,
    CoverFormat,
    HttpStream,
    MusicVideo,
    MusicVideoCodec,
    MusicVideoStreamSelector,
    SelectionKind,
    Song,
    SongCodec,
    SongStreamSelector,
    UploadedVideo,
    UploadedVideoQuality,
    UploadedVideoStreamSelector,
    VideoStream,
)
from gamdl.muxer import Muxer
from gamdl.stream import Streamer, StreamProgress

from .database import MediaDatabase
from .enums import (
    DecryptionMethod,
    FileStatus,
    MediaStatus,
    OverwritePolicy,
    PlaylistStatus,
)
from .events import DownloadEvent, DownloadEventCallback, DownloadStage, EventState
from .exceptions import DownloadFinalizationError
from .models import DatabaseResult, DownloadResult, FileResult, PlaylistResult
from .paths import PathFormatter, PlaylistTags
from .playlist import PlaylistWriter
from .publication import publish_bytes_result, publish_file
from .tag_resolver import TagResolver
from .tags import Tags


class _SidecarError(Exception):
    """Identify which optional sidecar operation failed, keeping its cause."""

    def __init__(self, stage: str):
        self.stage = stage
        super().__init__(stage)


def _tag_bytes(
    muxer: Muxer,
    data: bytes,
    suffix: str,
    tags: Tags,
    artwork: bytes | None,
) -> bytes:
    """Keep the tagger's temporary files entirely within one worker thread."""
    with TemporaryDirectory(prefix="gamdl-tag-") as directory:
        staged = Path(directory) / f"media{suffix}"
        staged.write_bytes(data)
        muxer.tag(staged, tags=tags, cover_bytes=artwork)
        return staged.read_bytes()


async def _complete_write(function: Callable[..., Any], /, *args: Any, **kwargs: Any):
    """Finish an atomic filesystem write even if its awaiting task is cancelled.

    Cancelling asyncio.to_thread() does not stop the underlying worker. Wait
    for publication/finalization to settle before propagating cancellation,
    so a caller never abandons an in-flight write after claiming it stopped.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A worker thread cannot be interrupted. Wait until it releases its
        # staged file before the TemporaryDirectory context can remove it.
        with suppress(Exception):
            await task
        raise


@dataclass(frozen=True)
class DownloadProgress:
    """A progress observation for one of a download's source tracks."""

    media_id: str
    track: Literal["audio", "video"]
    stream: StreamProgress


@dataclass(frozen=True)
class _PreparedMedia:
    data: bytes
    codec: SongCodec | MusicVideoCodec | UploadedVideoQuality
    methods: tuple[str, ...]
    cover_bytes: bytes | None = None


@dataclass(frozen=True)
class _PreparedFile:
    path: Path
    codec: SongCodec | MusicVideoCodec | UploadedVideoQuality | None
    methods: tuple[str, ...]
    cover_bytes: bytes | None = None
    resolved_tags: Tags | None = None


class Downloader:
    """Download, decrypt and tag Song, MusicVideo or UploadedVideo.

    ``get_bytes`` prepares in memory; ``download`` streams to private staged
    files, remuxes on disk and publishes atomically with bounded memory.
    Only ``download`` publishes a path and runs optional post-publication
    sidecars, index and playlist updates. Retrying a skipped file still runs
    those steps so interrupted finalization can be repaired.

    Caller-owned APIs and DRM objects are never closed here. The native
    Streamer owns and closes its own HTTP worker on every track.
    """

    def __init__(
        self,
        *,
        drm: Drm | None = None,
        wrapper_api: WrapperApi | None = None,
        itunes_api: ItunesApi | None = None,
        client: httpx.AsyncClient | None = None,
        tag_resolver: TagResolver | None = None,
        muxer: Muxer | None = None,
        database: MediaDatabase | None = None,
        path_formatter: PathFormatter | None = None,
        concurrency: int = 8,
        temp_path: str | os.PathLike[str] | None = None,
    ) -> None:
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError("concurrency must be a positive integer")
        if temp_path is not None and not os.fspath(temp_path).strip():
            raise ValueError("temp_path must be a nonempty path or None")
        self.drm = drm
        self.wrapper_api = wrapper_api
        self.itunes_api = itunes_api
        self.client = client
        self.tag_resolver = tag_resolver or TagResolver(
            wrapper_api=wrapper_api, itunes_api=itunes_api
        )
        self.muxer = muxer or Muxer()
        self.database = database
        self.path_formatter = path_formatter
        self.concurrency = concurrency
        self.temp_path = Path(temp_path).expanduser() if temp_path is not None else None

    @staticmethod
    def _emit(
        callback: DownloadEventCallback | None,
        media_id: str,
        stage: DownloadStage,
        state: EventState,
        *,
        track: Literal["audio", "video"] | None = None,
        progress: StreamProgress | None = None,
        result: DownloadResult | None = None,
    ) -> None:
        if callback is not None:
            callback(DownloadEvent(media_id, stage, state, track, progress, result))

    @classmethod
    @contextmanager
    def _stage(
        cls,
        callback: DownloadEventCallback | None,
        media_id: str,
        stage: DownloadStage,
        *,
        track: Literal["audio", "video"] | None = None,
    ) -> Iterator[None]:
        cls._emit(callback, media_id, stage, EventState.STARTED, track=track)
        try:
            yield
        except BaseException:
            cls._emit(callback, media_id, stage, EventState.FAILED, track=track)
            raise
        else:
            cls._emit(callback, media_id, stage, EventState.COMPLETED, track=track)

    async def _read_track(
        self,
        media_id: str,
        source: AudioStream | VideoStream | HttpStream,
        track: Literal["audio", "video"],
        progress_callback: Callable[[DownloadProgress], None] | None,
        *,
        event_callback: DownloadEventCallback | None = None,
    ) -> tuple[bytes, str]:
        chunks = []

        def report_key(state: str) -> None:
            self._emit(
                event_callback,
                media_id,
                DownloadStage.FETCHING_DECRYPTION_KEYS,
                EventState(state),
                track=track,
            )

        async with Streamer(
            source,
            concurrency=self.concurrency,
            drm=self.drm,
            wrapper_api=self.wrapper_api,
            key_event_callback=report_key if event_callback is not None else None,
        ) as streamer:
            with self._stage(
                event_callback, media_id, DownloadStage.DOWNLOADING, track=track
            ):
                async for chunk in streamer:
                    chunks.append(chunk)
                    if progress_callback is not None:
                        progress_callback(
                            DownloadProgress(media_id, track, streamer.progress)
                        )
                    self._emit(
                        event_callback,
                        media_id,
                        DownloadStage.DOWNLOADING,
                        EventState.PROGRESS,
                        track=track,
                        progress=streamer.progress,
                    )
                return (
                    await asyncio.to_thread(b"".join, chunks),
                    streamer.decryption_method,
                )

    async def _save_track(
        self,
        media_id: str,
        source: AudioStream | VideoStream | HttpStream,
        track: Literal["audio", "video"],
        destination: Path,
        progress_callback: Callable[[DownloadProgress], None] | None,
        *,
        event_callback: DownloadEventCallback | None = None,
    ) -> str:
        """Decrypt directly to a staged file; never collect track bytes."""

        def report_key(state: str) -> None:
            self._emit(
                event_callback,
                media_id,
                DownloadStage.FETCHING_DECRYPTION_KEYS,
                EventState(state),
                track=track,
            )

        def report_progress(snapshot: StreamProgress) -> None:
            if progress_callback is not None:
                progress_callback(DownloadProgress(media_id, track, snapshot))
            self._emit(
                event_callback,
                media_id,
                DownloadStage.DOWNLOADING,
                EventState.PROGRESS,
                track=track,
                progress=snapshot,
            )

        async with Streamer(
            source,
            concurrency=self.concurrency,
            drm=self.drm,
            wrapper_api=self.wrapper_api,
            key_event_callback=report_key if event_callback is not None else None,
        ) as streamer:
            with self._stage(
                event_callback, media_id, DownloadStage.DOWNLOADING, track=track
            ):
                await streamer.save(destination, progress_callback=report_progress)
            return streamer.decryption_method

    async def _prepare(
        self,
        media: Song | MusicVideo | UploadedVideo,
        *,
        codec: SongCodec | None,
        video_codec: MusicVideoCodec | None,
        codec_priority: Sequence[SongCodec | MusicVideoCodec] | None,
        max_height: int | None,
        ask: bool,
        selector: SongStreamSelector
        | MusicVideoStreamSelector
        | UploadedVideoStreamSelector
        | None,
        quality: UploadedVideoQuality | None,
        quality_priority: Sequence[UploadedVideoQuality] | None,
        container: Literal["m4v", "mp4"],
        tag: bool,
        exclude_tags: Sequence[str],
        include_cover: bool,
        cover_format: CoverFormat,
        progress_callback: Callable[[DownloadProgress], None] | None,
        event_callback: DownloadEventCallback | None = None,
        resolved_tags: Tags | None = None,
    ) -> _PreparedMedia:
        async def read(
            source: AudioStream | VideoStream | HttpStream,
            track: Literal["audio", "video"],
        ):
            if event_callback is None:
                return await self._read_track(
                    media.id, source, track, progress_callback
                )
            return await self._read_track(
                media.id,
                source,
                track,
                progress_callback,
                event_callback=event_callback,
            )

        if not isinstance(media, (Song, MusicVideo, UploadedVideo)):
            raise TypeError("Unsupported media type for Downloader")
        if container not in ("m4v", "mp4"):
            raise ValueError("container must be 'm4v' or 'mp4'")
        if not isinstance(media, UploadedVideo) and (
            quality is not None or quality_priority is not None
        ):
            raise ValueError(
                "quality preferences are only supported for uploaded videos"
            )
        if isinstance(media, Song):
            if video_codec is not None:
                raise ValueError("video_codec is only supported for music videos")
            if await media.is_uploaded():
                with self._stage(
                    event_callback, media.id, DownloadStage.FETCHING_STREAMS
                ):
                    source, suffix = await media.get_uploaded_stream()
                data, method = await read(source, "audio")
                selected_codec = None
                methods = (method,)
            else:
                with self._stage(
                    event_callback, media.id, DownloadStage.FETCHING_STREAMS
                ):
                    source = await media.get_stream(
                        codec=codec,
                        codec_priority=codec_priority,
                        ask=ask,
                        selector=selector,
                        wrapper_api=self.wrapper_api,
                        client=self.client,
                    )
                raw, method = await read(source, "audio")
                with self._stage(event_callback, media.id, DownloadStage.MUXING):
                    data = await asyncio.to_thread(self.muxer.remux, raw)
                selected_codec = source.codec
                methods = (method,)
                suffix = ".m4a"
        elif isinstance(media, MusicVideo):
            if codec is not None:
                raise ValueError("codec is only supported for songs")
            with self._stage(event_callback, media.id, DownloadStage.FETCHING_STREAMS):
                streams = await media.get_streams(
                    video_codec=video_codec,
                    codec_priority=codec_priority,
                    max_height=max_height,
                    ask=ask,
                    selector=selector,
                    wrapper_api=self.wrapper_api,
                    itunes_api=self.itunes_api,
                    client=self.client,
                )
            # asyncio.gather alone does not cancel the sibling when one fails.
            video_task = asyncio.create_task(read(streams.video, "video"))
            audio_task = asyncio.create_task(read(streams.audio, "audio"))
            tasks = (video_task, audio_task)
            try:
                (
                    (video_bytes, video_method),
                    (audio_bytes, audio_method),
                ) = await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            with self._stage(event_callback, media.id, DownloadStage.MUXING):
                data = await asyncio.to_thread(
                    self.muxer.mux,
                    video=video_bytes,
                    audio=audio_bytes,
                    container=container,
                )
            selected_codec = streams.video.codec
            methods = (video_method, audio_method)
            suffix = f".{container}"
        else:
            if (
                codec is not None
                or video_codec is not None
                or codec_priority is not None
            ):
                raise ValueError(
                    "Codec preferences are not supported for uploaded videos"
                )
            with self._stage(event_callback, media.id, DownloadStage.FETCHING_STREAMS):
                source = await media.get_stream(
                    quality=quality,
                    quality_priority=quality_priority,
                    ask=ask,
                    selector=selector,
                )
            # Progressive files already contain their audio/video tracks.
            # Preserve the original MP4 bytes and brand; never mux again.
            data, method = await read(source, "video")
            selected_codec = source.codec
            methods = (method,)
            suffix = f".{container}"

        artwork = None
        if tag:
            if resolved_tags is None:
                with self._stage(event_callback, media.id, DownloadStage.FETCHING_TAGS):
                    tags = (
                        await self.tag_resolver.get_tags(media, codec=selected_codec)
                        if isinstance(media, Song) and selected_codec is not None
                        else await self.tag_resolver.get_tags(media)
                    )
            else:
                tags = resolved_tags
            tags = replace(
                tags,
                **{
                    name: (() if name == "genre" else None)
                    for name in exclude_tags
                    if name != "cover" and name in Tags.__dataclass_fields__
                },
            )
            if (
                "lyrics" not in exclude_tags
                and isinstance(media, Song)
                and selected_codec is not None
            ):
                lyrics = await media.get_lyrics()
                if lyrics is not None:
                    tags = replace(tags, lyrics=lyrics.to_plain_text())
            if include_cover and "cover" not in exclude_tags:
                cover = await media.get_cover()
                if cover is not None:
                    artwork = await cover.get_cover_bytes(format=cover_format.value)
            # The current Rust tagger is file-based. This temporary output is
            # private; neither API publishes an untagged intermediate file.
            with self._stage(event_callback, media.id, DownloadStage.TAGGING):
                data = await asyncio.to_thread(
                    _tag_bytes, self.muxer, data, suffix, tags, artwork
                )
        return _PreparedMedia(data, selected_codec, methods, artwork)

    async def _prepare_file(
        self,
        media: Song | MusicVideo | UploadedVideo,
        directory: Path,
        *,
        codec: SongCodec | None,
        video_codec: MusicVideoCodec | None,
        codec_priority: Sequence[SongCodec | MusicVideoCodec] | None,
        max_height: int | None,
        ask: bool,
        selector: SongStreamSelector
        | MusicVideoStreamSelector
        | UploadedVideoStreamSelector
        | None,
        quality: UploadedVideoQuality | None,
        quality_priority: Sequence[UploadedVideoQuality] | None,
        container: Literal["m4v", "mp4"],
        tag: bool,
        exclude_tags: Sequence[str],
        include_cover: bool,
        cover_format: CoverFormat,
        progress_callback: Callable[[DownloadProgress], None] | None,
        event_callback: DownloadEventCallback | None = None,
        resolved_tags: Tags | None = None,
        selected_song_stream: AudioStream | None = None,
    ) -> _PreparedFile:
        """Prepare one media file on disk without accumulating its samples."""

        async def save(source, track, path):
            return await self._save_track(
                media.id,
                source,
                track,
                path,
                progress_callback,
                event_callback=event_callback,
            )

        if not isinstance(media, UploadedVideo) and (
            quality is not None or quality_priority is not None
        ):
            raise ValueError(
                "quality preferences are only supported for uploaded videos"
            )
        if isinstance(media, Song):
            if video_codec is not None:
                raise ValueError("video_codec is only supported for music videos")
            if await media.is_uploaded():
                with self._stage(
                    event_callback, media.id, DownloadStage.FETCHING_STREAMS
                ):
                    source, suffix = await media.get_uploaded_stream()
                prepared_path = directory / f"media{suffix}"
                method = await save(source, "audio", prepared_path)
                selected_codec = None
                methods = (method,)
            else:
                source = selected_song_stream
                if source is None:
                    with self._stage(
                        event_callback, media.id, DownloadStage.FETCHING_STREAMS
                    ):
                        source = await media.get_stream(
                            codec=codec,
                            codec_priority=codec_priority,
                            ask=ask,
                            selector=selector,
                            wrapper_api=self.wrapper_api,
                            client=self.client,
                        )
                raw = directory / "audio.fmp4"
                method = await save(source, "audio", raw)
                prepared_path = directory / "media.m4a"
                with self._stage(event_callback, media.id, DownloadStage.MUXING):
                    await _complete_write(self.muxer.remux_file, raw, prepared_path)
                selected_codec = source.codec
                methods = (method,)
        elif isinstance(media, MusicVideo):
            if codec is not None:
                raise ValueError("codec is only supported for music videos")
            with self._stage(event_callback, media.id, DownloadStage.FETCHING_STREAMS):
                streams = await media.get_streams(
                    video_codec=video_codec,
                    codec_priority=codec_priority,
                    max_height=max_height,
                    ask=ask,
                    selector=selector,
                    wrapper_api=self.wrapper_api,
                    itunes_api=self.itunes_api,
                    client=self.client,
                )
            raw_video = directory / "video.fmp4"
            raw_audio = directory / "audio.fmp4"
            video_task = asyncio.create_task(save(streams.video, "video", raw_video))
            audio_task = asyncio.create_task(save(streams.audio, "audio", raw_audio))
            tasks = (video_task, audio_task)
            try:
                video_method, audio_method = await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            prepared_path = directory / f"media.{container}"
            with self._stage(event_callback, media.id, DownloadStage.MUXING):
                await _complete_write(
                    self.muxer.mux_file,
                    video=raw_video,
                    audio=raw_audio,
                    destination=prepared_path,
                    container=container,
                )
            selected_codec = streams.video.codec
            methods = (video_method, audio_method)
        else:
            if (
                codec is not None
                or video_codec is not None
                or codec_priority is not None
            ):
                raise ValueError(
                    "Codec preferences are not supported for uploaded videos"
                )
            with self._stage(event_callback, media.id, DownloadStage.FETCHING_STREAMS):
                source = await media.get_stream(
                    quality=quality,
                    quality_priority=quality_priority,
                    ask=ask,
                    selector=selector,
                )
            prepared_path = directory / f"media.{container}"
            method = await save(source, "video", prepared_path)
            selected_codec = source.codec
            methods = (method,)

        artwork = None
        catalog_tags = resolved_tags
        if tag:
            if resolved_tags is None:
                with self._stage(event_callback, media.id, DownloadStage.FETCHING_TAGS):
                    tags = (
                        await self.tag_resolver.get_tags(media, codec=selected_codec)
                        if isinstance(media, Song) and selected_codec is not None
                        else await self.tag_resolver.get_tags(media)
                    )
            else:
                tags = resolved_tags
            catalog_tags = tags
            tags = replace(
                tags,
                **{
                    name: (() if name == "genre" else None)
                    for name in exclude_tags
                    if name != "cover" and name in Tags.__dataclass_fields__
                },
            )
            if (
                "lyrics" not in exclude_tags
                and isinstance(media, Song)
                and selected_codec is not None
            ):
                lyrics = await media.get_lyrics()
                if lyrics is not None:
                    tags = replace(tags, lyrics=lyrics.to_plain_text())
            if include_cover and "cover" not in exclude_tags:
                cover = await media.get_cover()
                if cover is not None:
                    artwork = await cover.get_cover_bytes(format=cover_format.value)
            with self._stage(event_callback, media.id, DownloadStage.TAGGING):
                await _complete_write(
                    self.muxer.tag, prepared_path, tags=tags, cover_bytes=artwork
                )
        return _PreparedFile(prepared_path, selected_codec, methods, artwork, catalog_tags)

    async def _save_sidecars(
        self,
        media: Song | MusicVideo | UploadedVideo,
        destination: Path,
        *,
        overwrite: OverwritePolicy,
        save_cover: bool,
        cover_format: CoverFormat,
        save_lyrics: bool,
        prepared_cover: bytes | None,
        event_callback: DownloadEventCallback | None = None,
        on_result: Callable[[str, FileResult], None] | None = None,
    ) -> tuple[FileResult | None, FileResult | None]:
        """Write only requested media sidecars; never alter the media file."""
        cover_result = None
        lyrics_result = None
        if save_cover:
            try:
                with self._stage(event_callback, media.id, DownloadStage.SAVING_COVER):
                    existing_cover = destination.with_suffix(
                        ".jpg" if cover_format == CoverFormat.JPG else ".png"
                    )
                    if overwrite == OverwritePolicy.SKIP and existing_cover.is_file():
                        # A known media file needs no Apple artwork request
                        # merely to confirm an existing sidecar.
                        cover_result = FileResult(existing_cover, FileStatus.SKIPPED)
                    else:
                        cover_bytes = prepared_cover
                        if cover_bytes is None:
                            cover = await media.get_cover()
                            cover_bytes = (
                                await cover.get_cover_bytes(format=cover_format.value)
                                if cover is not None
                                else None
                            )
                        if cover_bytes:
                            if cover_bytes.startswith(b"\xff\xd8\xff"):
                                extension = ".jpg"
                            elif cover_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                                extension = ".png"
                            else:
                                raise ValueError(
                                    "Cover sidecar must contain JPEG or PNG bytes"
                                )
                            cover_path = destination.with_suffix(extension)
                            status = await _complete_write(
                                publish_bytes_result,
                                cover_bytes,
                                cover_path,
                                overwrite=overwrite,
                            )
                            cover_result = FileResult(cover_path, status)
                        else:
                            cover_result = FileResult(None, FileStatus.UNAVAILABLE)
                    if on_result is not None:
                        on_result("cover", cover_result)
            except Exception as exc:
                raise _SidecarError("cover") from exc

        if save_lyrics:
            assert isinstance(media, Song)
            try:
                with self._stage(event_callback, media.id, DownloadStage.SAVING_LYRICS):
                    existing_lyrics = next(
                        (
                            sidecar
                            for suffix in (".lrc", ".txt")
                            if (sidecar := destination.with_suffix(suffix)).is_file()
                        ),
                        None,
                    )
                    if (
                        overwrite == OverwritePolicy.SKIP
                        and existing_lyrics is not None
                    ):
                        lyrics_result = FileResult(existing_lyrics, FileStatus.SKIPPED)
                    else:
                        lyrics = await media.get_lyrics()
                        if lyrics is not None:
                            if lyrics.is_synced:
                                sidecar = destination.with_suffix(".lrc")
                                contents = lyrics.to_lrc()
                            else:
                                sidecar = destination.with_suffix(".txt")
                                contents = lyrics.to_plain_text()
                            status = await _complete_write(
                                publish_bytes_result,
                                contents.encode("utf-8"),
                                sidecar,
                                overwrite=overwrite,
                            )
                            lyrics_result = FileResult(sidecar, status)
                        else:
                            lyrics_result = FileResult(None, FileStatus.UNAVAILABLE)
                    if on_result is not None:
                        on_result("lyrics", lyrics_result)
            except Exception as exc:
                raise _SidecarError("lyrics") from exc
        return cover_result, lyrics_result

    async def _finalize(
        self,
        media: Song | MusicVideo | UploadedVideo,
        result: DownloadResult,
        *,
        overwrite: OverwritePolicy,
        save_cover: bool,
        cover_format: CoverFormat,
        save_lyrics: bool,
        playlist_path: Path | None,
        playlist_tags: PlaylistTags | None,
        playlist_index: int | None,
        prepared_cover: bytes | None = None,
        resolved_tags: Tags | None = None,
        event_callback: DownloadEventCallback | None = None,
    ) -> DownloadResult:
        """Retryable, idempotent post-publication operations.

        If an operation fails, leave the finished file untouched and report
        its path. A subsequent call with ``overwrite=False`` replays only
        the finalization work, not the expensive media download.
        """

        def save_result(field: str, outcome: FileResult) -> None:
            nonlocal result
            result = replace(result, **{field: outcome})

        try:
            await self._save_sidecars(
                media,
                result.path,
                overwrite=overwrite,
                save_cover=save_cover,
                cover_format=cover_format,
                save_lyrics=save_lyrics,
                prepared_cover=prepared_cover,
                event_callback=event_callback,
                on_result=save_result,
            )
        except _SidecarError as exc:
            raise DownloadFinalizationError(
                result.path, exc.stage, result
            ) from exc.__cause__

        if self.database is not None:
            try:
                with self._stage(
                    event_callback, media.id, DownloadStage.UPDATING_DATABASE
                ):
                    db_status = await _complete_write(
                        self.database.record, media, result, resolved_tags
                    )
                result = replace(
                    result, database=DatabaseResult(self.database.path, db_status)
                )
            except Exception as exc:
                warnings.warn(
                    f"Download completed but database registration failed: {exc}",
                    stacklevel=2,
                )

        if playlist_path is not None:
            try:
                source = media.source
                position = playlist_index
                if (
                    position is None
                    and source is not None
                    and source.kind
                    in {
                        SelectionKind.PLAYLIST_TRACKS.value,
                        SelectionKind.ALBUM_TRACKS.value,
                    }
                ):
                    position = source.index
                with self._stage(
                    event_callback, media.id, DownloadStage.UPDATING_PLAYLIST
                ):
                    updated = await _complete_write(
                        PlaylistWriter(playlist_path, tags=playlist_tags).add,
                        result.path,
                        position=position,
                    )
                result = replace(
                    result,
                    playlist_file=PlaylistResult(
                        playlist_path,
                        PlaylistStatus.UPDATED if updated else PlaylistStatus.UNCHANGED,
                    ),
                )
            except Exception as exc:
                raise DownloadFinalizationError(
                    result.path, "playlist", result
                ) from exc
        self._emit(
            event_callback,
            media.id,
            DownloadStage.COMPLETED,
            EventState.COMPLETED,
            result=result,
        )
        return result

    async def get_bytes(
        self,
        media: Song | MusicVideo | UploadedVideo,
        *,
        codec: SongCodec | None = None,
        video_codec: MusicVideoCodec | None = None,
        codec_priority: Sequence[SongCodec | MusicVideoCodec] | None = None,
        max_height: int | None = 1080,
        ask: bool = False,
        selector: SongStreamSelector
        | MusicVideoStreamSelector
        | UploadedVideoStreamSelector
        | None = None,
        quality: UploadedVideoQuality | None = None,
        quality_priority: Sequence[UploadedVideoQuality] | None = None,
        container: Literal["m4v", "mp4"] = "m4v",
        tag: bool = True,
        exclude_tags: Sequence[str] = (),
        include_cover: bool = True,
        cover_format: CoverFormat | str = CoverFormat.JPG,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
        event_callback: DownloadEventCallback | None = None,
    ) -> bytes:
        """Return finished MP4-family bytes without publishing a media file."""
        cover_format = CoverFormat(cover_format)
        prepared = await self._prepare(
            media,
            codec=codec,
            video_codec=video_codec,
            codec_priority=codec_priority,
            max_height=max_height,
            ask=ask,
            selector=selector,
            quality=quality,
            quality_priority=quality_priority,
            container=container,
            tag=tag,
            exclude_tags=exclude_tags,
            include_cover=include_cover,
            cover_format=cover_format,
            progress_callback=progress_callback,
            event_callback=event_callback,
        )
        self._emit(
            event_callback, media.id, DownloadStage.COMPLETED, EventState.COMPLETED
        )
        return prepared.data

    async def download(
        self,
        media: Song | MusicVideo | UploadedVideo,
        output: str | os.PathLike[str] | None = None,
        *,
        overwrite: bool = False,
        codec: SongCodec | None = None,
        video_codec: MusicVideoCodec | None = None,
        codec_priority: Sequence[SongCodec | MusicVideoCodec] | None = None,
        max_height: int | None = 1080,
        ask: bool = False,
        selector: SongStreamSelector
        | MusicVideoStreamSelector
        | UploadedVideoStreamSelector
        | None = None,
        quality: UploadedVideoQuality | None = None,
        quality_priority: Sequence[UploadedVideoQuality] | None = None,
        playlist_tags: PlaylistTags | None = None,
        video_extension: Literal[".m4v", ".mp4"] = ".m4v",
        tag: bool = True,
        exclude_tags: Sequence[str] = (),
        include_cover: bool = True,
        cover_format: CoverFormat | str = CoverFormat.JPG,
        save_cover: bool = False,
        save_lyrics: bool = False,
        playlist_path: str | os.PathLike[str] | None = None,
        playlist_index: int | None = None,
        save_playlist: bool = False,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
        event_callback: DownloadEventCallback | None = None,
    ) -> DownloadResult:
        """Atomically publish a song M4A or video M4V/MP4 at ``output``.

        An explicit .mp4 video path opts into mp42 branding. Automatically
        generated video paths use Gamdl 3.x's numbered suffixes (Title 2.m4v)
        and reuse files whose catalog ID matches an index entry or MP4 cnID.
        ``overwrite=False`` avoids media transfer, but still performs requested
        sidecar/playlist/database finalization. Sidecars, playlist entries and
        index updates are opt-in and idempotent; ``get_bytes`` never performs them.
        Tagged catalog songs embed plain-text lyrics by default. Exclude the
        ``lyrics`` tag through ``exclude_tags`` to omit it; ``save_lyrics``
        separately controls whether a synchronized/plain-text sidecar is saved.
        """
        if not isinstance(media, (Song, MusicVideo, UploadedVideo)):
            raise TypeError("Unsupported media type for Downloader")
        cover_format = CoverFormat(cover_format)
        if video_extension not in (".m4v", ".mp4"):
            raise ValueError("video_extension must be '.m4v' or '.mp4'")
        if type(overwrite) is not bool:
            raise TypeError("overwrite must be a boolean")
        overwrite = OverwritePolicy.REPLACE if overwrite else OverwritePolicy.SKIP
        if save_lyrics and not isinstance(media, Song):
            raise ValueError("save_lyrics is supported for songs only")
        if playlist_index is not None and (
            (playlist_path is None and not save_playlist)
            or type(playlist_index) is not int
            or playlist_index < 0
        ):
            raise ValueError(
                "playlist_index requires a playlist path and non-negative index"
            )
        if save_playlist and playlist_path is None:
            if self.path_formatter is None:
                raise ValueError("save_playlist requires a PathFormatter")
            if playlist_tags is None:
                source = media.source
                if source is None or source.playlist is None:
                    raise ValueError(
                        "save_playlist requires media.source.playlist "
                        "or explicit playlist_tags"
                    )
                playlist = await source.playlist.get_metadata()
                playlist_tags = PlaylistTags(
                    title=playlist.title,
                    artist=playlist.artist,
                    playlist_id=playlist.id,
                    track=(
                        playlist_index if playlist_index is not None else source.index
                    )
                    + 1,
                )
            playlist_path = self.path_formatter.get_playlist_path(playlist_tags)
        if (
            playlist_index is None
            and playlist_tags is not None
            and playlist_tags.track is not None
        ):
            if type(playlist_tags.track) is not int or playlist_tags.track < 1:
                raise ValueError(
                    "playlist_tags.track must be a positive one-based integer"
                )
            playlist_index = playlist_tags.track - 1
        # Recover 3.x's database early exit: catalog/playlist metadata already
        # contains the media ID, so a confirmed local file needs no tag,
        # playback, stream or decryption requests.  Keep explicit codec and
        # quality-selection requests on the normal path, and never reuse a
        # different output directory or video container by accident.
        if (
            output is None
            and self.path_formatter is not None
            and self.database is not None
            and overwrite == OverwritePolicy.SKIP
            and not ask
            and codec is None
            and video_codec is None
        ):
            if isinstance(media, MusicVideo):
                suffixes = (video_extension,)
            elif isinstance(media, Song):
                suffixes = (".mp3", ".m4a") if media.is_library else (".m4a",)
            else:
                suffixes = ()
            if suffixes:
                # A single-codec priority expresses an exact preference. For
                # multiple priorities, skip existing media like 3.x; explicit
                # replacement handles changing an older encode.
                required_codec = (
                    getattr(codec_priority[0], "value", str(codec_priority[0]))
                    if codec_priority is not None and len(codec_priority) == 1
                    else None
                )
                existing = await asyncio.to_thread(
                    self.database.find_by_media_id,
                    media.type,
                    media.id,
                    root=self.path_formatter.output_dir,
                    extensions=suffixes,
                    codec=required_codec,
                )
                if existing is not None:
                    return await self._finalize(
                        media,
                        DownloadResult(
                            media.id,
                            Path(existing["path"]),
                            MediaStatus.SKIPPED,
                            0,
                            None,
                        ),
                        overwrite=overwrite,
                        save_cover=save_cover,
                        cover_format=cover_format,
                        save_lyrics=save_lyrics,
                        playlist_path=Path(playlist_path)
                        if playlist_path is not None
                        else None,
                        playlist_tags=playlist_tags,
                        playlist_index=playlist_index,
                        resolved_tags=None,
                        event_callback=event_callback,
                    )
        generated_video_path: Path | None = None
        resolved_tags: Tags | None = None
        selected_song_stream: AudioStream | None = None
        uploaded_song = isinstance(media, Song) and await media.is_uploaded()
        uploaded_extension = None
        if uploaded_song:
            _, uploaded_extension = await media.get_uploaded_stream()
            save_lyrics = False
        if output is None:
            if self.path_formatter is None:
                raise ValueError("output is required without a PathFormatter")
            song_tag_codec = None
            if isinstance(media, Song) and not uploaded_song:
                # In mixed-priority or interactive mode, the actual selected
                # codec determines whether tags come from wrapper or web.
                # Reuse this selected stream during download; do not prompt or
                # fetch a second stream once the output path is determined.
                priority_codecs = (
                    tuple(SongCodec(item) for item in codec_priority)
                    if codec_priority is not None
                    else ()
                )
                if ask or len({item.is_web for item in priority_codecs}) > 1:
                    with self._stage(
                        event_callback, media.id, DownloadStage.FETCHING_STREAMS
                    ):
                        selected_song_stream = await media.get_stream(
                            codec=codec,
                            codec_priority=codec_priority,
                            ask=ask,
                            selector=selector,
                            wrapper_api=self.wrapper_api,
                            client=self.client,
                        )
                    song_tag_codec = selected_song_stream.codec
                else:
                    song_tag_codec = (
                        SongCodec(codec)
                        if codec is not None
                        else priority_codecs[0]
                        if priority_codecs
                        else SongCodec.AAC_WEB
                    )
            with self._stage(event_callback, media.id, DownloadStage.FETCHING_TAGS):
                tags = (
                    await self.tag_resolver.get_tags(media, codec=song_tag_codec)
                    if song_tag_codec is not None
                    else await self.tag_resolver.get_tags(media)
                )
                resolved_tags = tags
            extension = (
                (uploaded_extension or ".m4a")
                if isinstance(media, Song)
                else video_extension
            )
            destination = self.path_formatter.get_media_path(
                media, tags, extension=extension, playlist=playlist_tags
            )
            if isinstance(media, (MusicVideo, UploadedVideo)):
                generated_video_path = destination
                destination = await asyncio.to_thread(
                    self.path_formatter.resolve_video_path,
                    destination,
                    media,
                    database=self.database,
                )
        else:
            destination = Path(output)
        if isinstance(media, Song):
            if destination.suffix.lower() != (uploaded_extension or ".m4a"):
                raise ValueError(
                    f"Song output must end in {uploaded_extension or '.m4a'}"
                )
            container: Literal["m4v", "mp4"] = "m4v"
        else:
            if destination.suffix.lower() not in (".m4v", ".mp4"):
                raise ValueError("Video output must end in .m4v or .mp4")
            container = "mp4" if destination.suffix.lower() == ".mp4" else "m4v"

        destination_existed = await asyncio.to_thread(destination.exists)
        if destination_existed and overwrite == OverwritePolicy.SKIP:
            return await self._finalize(
                media,
                DownloadResult(media.id, destination, MediaStatus.SKIPPED, 0, None),
                overwrite=overwrite,
                save_cover=save_cover,
                cover_format=cover_format,
                save_lyrics=save_lyrics,
                playlist_path=Path(playlist_path)
                if playlist_path is not None
                else None,
                playlist_tags=playlist_tags,
                playlist_index=playlist_index,
                resolved_tags=resolved_tags,
                event_callback=event_callback,
            )

        # System temporary directories may be RAM-backed (e.g. /tmp on Arch).
        # Default to the destination filesystem for bounded-RAM staging and
        # cheap atomic hardlink publication. An explicit temp_path overrides it.
        scratch_root = self.temp_path or destination.parent
        await asyncio.to_thread(scratch_root.mkdir, parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="gamdl-", dir=scratch_root) as directory:
            prepared = await self._prepare_file(
                media,
                Path(directory),
                codec=codec,
                video_codec=video_codec,
                codec_priority=codec_priority,
                max_height=max_height,
                ask=ask,
                selector=selector,
                quality=quality,
                quality_priority=quality_priority,
                container=container,
                tag=tag,
                exclude_tags=exclude_tags,
                include_cover=include_cover,
                cover_format=cover_format,
                progress_callback=progress_callback,
                event_callback=event_callback,
                resolved_tags=resolved_tags,
                selected_song_stream=selected_song_stream,
            )
            # A newly created generated video slot must never be replaced
            # even when the user requested overwrite=True.
            publish_policy = (
                OverwritePolicy.SKIP
                if generated_video_path is not None
                and overwrite == OverwritePolicy.REPLACE
                and not destination_existed
                else overwrite
            )
            with self._stage(event_callback, media.id, DownloadStage.PUBLISHING):
                published = await _complete_write(
                    publish_file,
                    prepared.path,
                    destination,
                    overwrite=publish_policy,
                )
            if (
                not published
                and generated_video_path is not None
                and publish_policy == OverwritePolicy.SKIP
            ):
                # A different ID may have claimed the filename; retry with
                # the same staged media without re-downloading or re-muxing.
                for _ in range(20):
                    candidate = await asyncio.to_thread(
                        self.path_formatter.resolve_video_path,
                        generated_video_path,
                        media,
                        database=self.database,
                    )
                    if candidate == destination:
                        break  # A concurrent download of the same ID won.
                    destination = candidate
                    with self._stage(
                        event_callback, media.id, DownloadStage.PUBLISHING
                    ):
                        published = await _complete_write(
                            publish_file,
                            prepared.path,
                            destination,
                            overwrite=OverwritePolicy.SKIP,
                        )
                    if published:
                        break
                else:
                    raise RuntimeError("Unable to resolve a stable video filename")
            size = prepared.path.stat().st_size if published else 0
            result = (
                DownloadResult(
                    media.id,
                    destination,
                    MediaStatus.OVERWRITTEN
                    if overwrite == OverwritePolicy.REPLACE and destination_existed
                    else MediaStatus.DOWNLOADED,
                    size,
                    prepared.codec,
                    tuple(DecryptionMethod(method) for method in prepared.methods),
                )
                if published
                else DownloadResult(media.id, destination, MediaStatus.SKIPPED, 0, None)
            )
        return await self._finalize(
            media,
            result,
            overwrite=overwrite,
            save_cover=save_cover,
            cover_format=cover_format,
            save_lyrics=save_lyrics,
            playlist_path=Path(playlist_path) if playlist_path is not None else None,
            playlist_tags=playlist_tags,
            playlist_index=playlist_index,
            prepared_cover=prepared.cover_bytes,
            resolved_tags=prepared.resolved_tags,
            event_callback=event_callback,
        )
