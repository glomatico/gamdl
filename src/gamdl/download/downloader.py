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
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Literal

import httpx

from gamdl.api import ItunesApi
from gamdl.api.wrapper import WrapperApi
from gamdl.drm import Drm
from gamdl.media import (
    AudioStream,
    MusicVideo,
    MusicVideoCodec,
    MusicVideoStreamSelector,
    Song,
    SongCodec,
    SongStreamSelector,
    VideoStream,
)
from gamdl.muxer import Muxer
from gamdl.stream import Streamer, StreamProgress

from .tag_resolver import TagResolver


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
    codec: SongCodec | MusicVideoCodec | None
    decryption_methods: tuple[str, ...] = ()


@dataclass(frozen=True)
class _PreparedMedia:
    data: bytes
    codec: SongCodec | MusicVideoCodec
    methods: tuple[str, ...]


class Downloader:
    """Download, decrypt, remux, and optionally tag one Song or MusicVideo.

    The same preparation path powers ``get_bytes`` and atomic ``download``.
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
        self.concurrency = concurrency

    async def _read_track(
        self,
        media_id: str,
        source: AudioStream | VideoStream,
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
        media: Song | MusicVideo,
        *,
        codec: SongCodec | None,
        video_codec: MusicVideoCodec | None,
        codec_priority: Sequence[SongCodec | MusicVideoCodec] | None,
        max_height: int | None,
        ask: bool,
        selector: SongStreamSelector | MusicVideoStreamSelector | None,
        container: Literal["m4v", "mp4"],
        tag: bool,
        include_cover: bool,
        include_lyrics: bool,
        progress_callback: Callable[[DownloadProgress], None] | None,
    ) -> _PreparedMedia:
        if not isinstance(media, (Song, MusicVideo)):
            raise TypeError("Downloader supports Song and MusicVideo only")
        if container not in ("m4v", "mp4"):
            raise ValueError("container must be 'm4v' or 'mp4'")
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
        else:
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

        if tag:
            tags = await self.tag_resolver.get_tags(media)
            if include_lyrics and isinstance(media, Song):
                lyrics = await media.get_lyrics()
                if lyrics is not None:
                    tags = replace(tags, lyrics=lyrics.to_plain_text())
            artwork = None
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
        return _PreparedMedia(data, selected_codec, methods)

    async def get_bytes(
        self,
        media: Song | MusicVideo,
        *,
        codec: SongCodec | None = None,
        video_codec: MusicVideoCodec | None = None,
        codec_priority: Sequence[SongCodec | MusicVideoCodec] | None = None,
        max_height: int | None = 1080,
        ask: bool = False,
        selector: SongStreamSelector | MusicVideoStreamSelector | None = None,
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
            container=container,
            tag=tag,
            include_cover=include_cover,
            include_lyrics=include_lyrics,
            progress_callback=progress_callback,
        )
        return prepared.data

    async def download(
        self,
        media: Song | MusicVideo,
        output: str | os.PathLike[str],
        *,
        overwrite: Literal["skip", "replace", "error"] = "skip",
        codec: SongCodec | None = None,
        video_codec: MusicVideoCodec | None = None,
        codec_priority: Sequence[SongCodec | MusicVideoCodec] | None = None,
        max_height: int | None = 1080,
        ask: bool = False,
        selector: SongStreamSelector | MusicVideoStreamSelector | None = None,
        tag: bool = True,
        include_cover: bool = True,
        include_lyrics: bool = False,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
    ) -> DownloadResult:
        """Atomically publish a song M4A or video M4V/MP4 at ``output``.

        An explicit .mp4 video path opts into mp42 branding. ``skip`` avoids
        download if the destination exists; it also handles concurrent
        publications without overwriting a winner. ``replace`` uses atomic
        os.replace. All failures/cancellations clean up staged output.
        """
        if not isinstance(media, (Song, MusicVideo)):
            raise TypeError("Downloader supports Song and MusicVideo only")
        if overwrite not in ("skip", "replace", "error"):
            raise ValueError("overwrite must be 'skip', 'replace', or 'error'")
        destination = Path(output)
        if isinstance(media, Song):
            if destination.suffix.lower() != ".m4a":
                raise ValueError("Song output must end in .m4a")
            container: Literal["m4v", "mp4"] = "m4v"
        else:
            if destination.suffix.lower() not in (".m4v", ".mp4"):
                raise ValueError("Music-video output must end in .m4v or .mp4")
            container = "mp4" if destination.suffix.lower() == ".mp4" else "m4v"

        if destination.exists():
            if overwrite == "skip":
                return DownloadResult(media.id, destination, "skipped", 0, None)
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
            container=container,
            tag=tag,
            include_cover=include_cover,
            include_lyrics=include_lyrics,
            progress_callback=progress_callback,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        staged: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="wb",
                prefix=f".{destination.name}.",
                suffix=".part",
                dir=destination.parent,
                delete=False,
            ) as temporary:
                staged = Path(temporary.name)
                temporary.write(prepared.data)
            if overwrite == "replace":
                staged.replace(destination)
            else:
                try:
                    os.link(staged, destination)
                except FileExistsError:
                    if overwrite == "error":
                        raise
                    return DownloadResult(media.id, destination, "skipped", 0, None)
            return DownloadResult(
                media.id,
                destination,
                "downloaded",
                len(prepared.data),
                prepared.codec,
                prepared.methods,
            )
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
