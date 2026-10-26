"""Single-item downloads built from the public media, stream and mux layers.

No global login or terminal UI: the caller provides an authenticated media
object, any DRM/wrapper services, output options, and optional UI callbacks.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

import httpx

from gamdl.api import ItunesApi
from gamdl.api.wrapper import WrapperApi
from gamdl.drm import Drm
from gamdl.media import (
    AudioStream,
    HttpStream,
    MusicVideo,
    MusicVideoCodec,
    MusicVideoStreamSelector,
    UploadedVideo,
    UploadedVideoQuality,
    UploadedVideoStreamSelector,
    SelectionKind,
    Song,
    SongCodec,
    SongStreamSelector,
    VideoStream,
)
from gamdl.muxer import Muxer
from gamdl.stream import Streamer, StreamProgress

from .tag_resolver import TagResolver
from .database import MediaDatabase
from .exceptions import DownloadFinalizationError
from .playlist import PlaylistWriter
from .publication import publish_bytes
from .paths import PathFormatter, PlaylistTags
from .path_resolver import resolve_numbered_video_path, resolve_song_path


@dataclass(frozen=True)
class DownloadProgress:
    """A progress observation for one of a download's source tracks."""

    media_id: str
    track: Literal["audio", "video"]
    stream: StreamProgress


@dataclass(frozen=True)
class DownloadResult:
    """Result of publishing a single media file; no credentials or signed URLs."""

    media_id: str
    path: Path
    status: Literal["downloaded", "skipped"]
    bytes_written: int
    codec: SongCodec | MusicVideoCodec | UploadedVideoQuality | None
    decryption_methods: tuple[str, ...] = ()


@dataclass(frozen=True)
class _PreparedMedia:
    data: bytes
    codec: SongCodec | MusicVideoCodec | UploadedVideoQuality
    methods: tuple[str, ...]
    cover_bytes: bytes | None = None


class Downloader:
    """Download, decrypt and tag Song, MusicVideo or UploadedVideo.

    The same preparation path powers ``get_bytes`` and atomic ``download``.
    Only ``download`` publishes a path and runs optional post-publication
    sidecars, index and playlist updates. Retrying a skipped file still runs
    those steps so interrupted finalization can be repaired.
    Existing bytes-first muxing temporarily holds full tracks in memory;
    file-backed muxing is a separate planned optimization.

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
    ) -> None:
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError("concurrency must be a positive integer")
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

    async def _read_track(
        self,
        media_id: str,
        source: AudioStream | VideoStream | HttpStream,
        track: Literal["audio", "video"],
        progress_callback: Callable[[DownloadProgress], None] | None,
    ) -> tuple[bytes, str]:
        chunks = []
        async with Streamer(
            source,
            concurrency=self.concurrency,
            drm=self.drm,
            wrapper_api=self.wrapper_api,
        ) as streamer:
            async for chunk in streamer:
                chunks.append(chunk)
                if progress_callback is not None:
                    progress_callback(
                        DownloadProgress(media_id, track, streamer.progress)
                    )
            return b"".join(chunks), streamer.decryption_method

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
        include_cover: bool,
        include_lyrics: bool,
        progress_callback: Callable[[DownloadProgress], None] | None,
    ) -> _PreparedMedia:
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
            source = await media.get_stream(
                codec=codec,
                codec_priority=codec_priority,
                ask=ask,
                selector=selector,
                wrapper_api=self.wrapper_api,
                client=self.client,
            )
            raw, method = await self._read_track(
                media.id, source, "audio", progress_callback
            )
            data = self.muxer.remux(raw)
            selected_codec = source.codec
            methods = (method,)
            suffix = ".m4a"
        elif isinstance(media, MusicVideo):
            if codec is not None:
                raise ValueError("codec is only supported for songs")
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
            video_task = asyncio.create_task(
                self._read_track(media.id, streams.video, "video", progress_callback)
            )
            audio_task = asyncio.create_task(
                self._read_track(media.id, streams.audio, "audio", progress_callback)
            )
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
            data = self.muxer.mux(
                video=video_bytes, audio=audio_bytes, container=container
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
            source = await media.get_stream(
                quality=quality,
                quality_priority=quality_priority,
                ask=ask,
                selector=selector,
            )
            # Progressive files already contain their audio/video tracks.
            # Preserve the original MP4 bytes and brand; never mux again.
            data, method = await self._read_track(
                media.id, source, "video", progress_callback
            )
            selected_codec = source.codec
            methods = (method,)
            suffix = f".{container}"

        artwork = None
        if tag:
            tags = await self.tag_resolver.get_tags(media)
            if include_lyrics and isinstance(media, Song):
                lyrics = await media.get_lyrics()
                if lyrics is not None:
                    tags = replace(tags, lyrics=lyrics.to_plain_text())
            if include_cover:
                cover = await media.get_cover()
                if cover is not None:
                    artwork = await cover.get_cover_bytes()
            # The current Rust tagger is file-based. This temporary output is
            # private; neither API publishes an untagged intermediate file.
            with TemporaryDirectory(prefix="gamdl-tag-") as directory:
                staged = Path(directory) / f"media{suffix}"
                staged.write_bytes(data)
                self.muxer.tag(staged, tags=tags, cover_bytes=artwork)
                data = staged.read_bytes()
        return _PreparedMedia(data, selected_codec, methods, artwork)

    async def _save_sidecars(
        self,
        media: Song | MusicVideo | UploadedVideo,
        destination: Path,
        *,
        overwrite: str,
        save_cover: bool,
        save_lyrics: bool,
        prepared_cover: bytes | None,
    ) -> None:
        """Write only requested media sidecars; never alter the media file."""
        if save_cover:
            cover_bytes = prepared_cover
            if cover_bytes is None:
                cover = await media.get_cover()
                cover_bytes = (
                    await cover.get_cover_bytes() if cover is not None else None
                )
            if cover_bytes:
                if cover_bytes.startswith(b"\xff\xd8\xff"):
                    extension = ".jpg"
                elif cover_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                    extension = ".png"
                else:
                    raise ValueError("Cover sidecar must contain JPEG or PNG bytes")
                cover_path = destination.with_suffix(extension)
                if overwrite != "skip" or not cover_path.exists():
                    publish_bytes(cover_bytes, cover_path, overwrite=overwrite)

        if save_lyrics:
            assert isinstance(media, Song)
            lyrics = await media.get_lyrics()
            if lyrics is not None:
                if lyrics.is_synced:
                    sidecar = destination.with_suffix(".lrc")
                    contents = lyrics.to_lrc()
                else:
                    sidecar = destination.with_suffix(".txt")
                    contents = lyrics.to_plain_text()
                if overwrite != "skip" or not sidecar.exists():
                    publish_bytes(
                        contents.encode("utf-8"), sidecar, overwrite=overwrite
                    )

    async def _finalize(
        self,
        media: Song | MusicVideo | UploadedVideo,
        result: DownloadResult,
        *,
        overwrite: str,
        save_cover: bool,
        save_lyrics: bool,
        playlist_path: Path | None,
        playlist_index: int | None,
        prepared_cover: bytes | None = None,
    ) -> DownloadResult:
        """Retryable, idempotent post-publication operations.

        If an operation fails, leave the finished file untouched and report
        its path. A subsequent call with ``overwrite='skip'`` replays only
        the finalization work, not the expensive media download.
        """
        try:
            await self._save_sidecars(
                media,
                result.path,
                overwrite=overwrite,
                save_cover=save_cover,
                save_lyrics=save_lyrics,
                prepared_cover=prepared_cover,
            )
        except Exception as exc:
            raise DownloadFinalizationError(result.path, "sidecar") from exc

        if self.database is not None:
            try:
                self.database.record(media, result)
            except Exception as exc:
                raise DownloadFinalizationError(result.path, "database") from exc

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
                PlaylistWriter(playlist_path).add(result.path, position=position)
            except Exception as exc:
                raise DownloadFinalizationError(result.path, "playlist") from exc
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
        include_cover: bool = True,
        include_lyrics: bool = False,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> bytes:
        """Return finished MP4-family bytes without publishing a media file."""
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
            include_cover=include_cover,
            include_lyrics=include_lyrics,
            progress_callback=progress_callback,
        )
        return prepared.data

    async def download(
        self,
        media: Song | MusicVideo | UploadedVideo,
        output: str | os.PathLike[str] | None = None,
        *,
        overwrite: Literal["skip", "replace", "error"] = "skip",
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
        include_cover: bool = True,
        include_lyrics: bool = False,
        save_cover: bool = False,
        save_lyrics: bool = False,
        playlist_path: str | os.PathLike[str] | None = None,
        playlist_index: int | None = None,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> DownloadResult:
        """Atomically publish a song M4A or video M4V/MP4 at ``output``.

        An explicit .mp4 video path opts into mp42 branding. Automatically
        generated video paths use GAMDL 3.x's numbered suffixes (Title 2.m4v)
        and reuse files whose catalog ID matches an index entry or MP4 cnID.
        ``skip`` avoids media transfer, but still performs requested
        sidecar/playlist/database finalization. Sidecars, playlist entries and
        index updates are opt-in and idempotent; ``get_bytes`` never performs them.
        """
        if not isinstance(media, (Song, MusicVideo, UploadedVideo)):
            raise TypeError("Unsupported media type for Downloader")
        if video_extension not in (".m4v", ".mp4"):
            raise ValueError("video_extension must be '.m4v' or '.mp4'")
        if overwrite not in ("skip", "replace", "error"):
            raise ValueError("overwrite must be 'skip', 'replace', or 'error'")
        if save_lyrics and not isinstance(media, Song):
            raise ValueError("save_lyrics is supported for songs only")
        if playlist_index is not None and (
            playlist_path is None
            or type(playlist_index) is not int
            or playlist_index < 0
        ):
            raise ValueError(
                "playlist_index requires a playlist path and non-negative index"
            )
        generated_video_path: Path | None = None
        if output is None:
            if self.path_formatter is None:
                raise ValueError("output is required without a PathFormatter")
            tags = await self.tag_resolver.get_tags(media)
            extension = ".m4a" if isinstance(media, Song) else video_extension
            destination = self.path_formatter.get_media_path(
                media, tags, extension=extension, playlist=playlist_tags
            )
            if isinstance(media, (MusicVideo, UploadedVideo)):
                generated_video_path = destination
                destination = resolve_numbered_video_path(
                    destination, media, database=self.database
                )
            else:
                # PathFormatter is pure. Filesystem identity checks belong to
                # this resolver, using the same native cnID reader as videos.
                destination = resolve_song_path(
                    destination,
                    media,
                    catalog_id=(
                        str(tags.title_id)
                        if tags.title_id is not None
                        else media.id
                        if media.id.isdecimal()
                        else None
                    ),
                    database=self.database,
                )
        else:
            destination = Path(output)
        if isinstance(media, Song):
            if destination.suffix.lower() != ".m4a":
                raise ValueError("Song output must end in .m4a")
            container: Literal["m4v", "mp4"] = "m4v"
        else:
            if destination.suffix.lower() not in (".m4v", ".mp4"):
                raise ValueError("Video output must end in .m4v or .mp4")
            container = "mp4" if destination.suffix.lower() == ".mp4" else "m4v"

        destination_existed = destination.exists()
        if destination_existed:
            if overwrite == "skip":
                return await self._finalize(
                    media,
                    DownloadResult(media.id, destination, "skipped", 0, None),
                    overwrite=overwrite,
                    save_cover=save_cover,
                    save_lyrics=save_lyrics,
                    playlist_path=Path(playlist_path)
                    if playlist_path is not None
                    else None,
                    playlist_index=playlist_index,
                )
            if overwrite == "error":
                raise FileExistsError(destination)

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
            include_cover=include_cover,
            include_lyrics=include_lyrics,
            progress_callback=progress_callback,
        )
        # A previously empty generated video slot is never safe to replace:
        # another process may have claimed it while we downloaded the media.
        # `replace` still replaces a known same-ID video that existed before
        # preparation; only *new* generated slots use no-clobber publication.
        publish_policy = (
            "skip"
            if generated_video_path is not None
            and overwrite == "replace"
            and not destination_existed
            else overwrite
        )
        published = publish_bytes(prepared.data, destination, overwrite=publish_policy)
        if (
            not published
            and generated_video_path is not None
            and publish_policy == "skip"
        ):
            # The planned filename may have been claimed by another process
            # during download. Never finalize it under the wrong media ID;
            # resolve the updated occupied slots, then publish elsewhere.
            for _ in range(20):
                candidate = resolve_numbered_video_path(
                    generated_video_path, media, database=self.database
                )
                if candidate == destination:
                    # A competing download of this *same* catalog ID won.
                    break
                destination = candidate
                published = publish_bytes(prepared.data, destination, overwrite="skip")
                if published:
                    break
            else:
                raise RuntimeError("Unable to resolve a stable video filename")
        result = (
            DownloadResult(
                media.id,
                destination,
                "downloaded",
                len(prepared.data),
                prepared.codec,
                prepared.methods,
            )
            if published
            else DownloadResult(media.id, destination, "skipped", 0, None)
        )
        return await self._finalize(
            media,
            result,
            overwrite=overwrite,
            save_cover=save_cover,
            save_lyrics=save_lyrics,
            playlist_path=Path(playlist_path) if playlist_path is not None else None,
            playlist_index=playlist_index,
            prepared_cover=prepared.cover_bytes,
        )
