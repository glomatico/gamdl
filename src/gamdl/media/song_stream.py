"""Apple Music song stream discovery and codec selection.

Separate finding a playback/HLS URL from interpreting its media playlist.
No DRM keys, media segments, or licenses are fetched by discovery.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from ..api.exceptions import GamdlApiError
from .exceptions import SelectionRequiredError, StreamNotAvailableError
from .stream import (
    WEB_FLAVORS,
    AudioStream,
    HlsSongStream,
    SongCodec,
    StreamOrigin,
    StreamTransport,
    WebPlaybackStream,
    match_hls_codec,
)

if TYPE_CHECKING:
    from ..api.wrapper import WrapperApi
    from .song import Song


@dataclass(frozen=True)
class StreamSelectionOption:
    """A stream candidate exposed to a caller-owned UI callback."""

    index: int
    stream: AudioStream

    @property
    def codec(self) -> SongCodec:
        return self.stream.codec

    @property
    def origin(self) -> StreamOrigin:
        return self.stream.origin

    @property
    def bitrate(self) -> int | None:
        return self.stream.bitrate


@dataclass(frozen=True)
class StreamSelectionRequest:
    """Unified Webplayback and HLS selection request, independent of CLI."""

    media_id: str
    options: tuple[StreamSelectionOption, ...]


StreamSelector = Callable[[StreamSelectionRequest], int | Awaitable[int]]


def _default_master_url(value: str) -> str:
    """Match Gamdl 3.x's HLS P-number default-variant normalization."""
    return re.sub(r"(P\d+)_[^/?]+(?=\.m3u8)", r"\1_default", value)


def _first_song(playback: dict) -> dict:
    entries = playback.get("songList")
    return (
        entries[0]
        if isinstance(entries, list) and entries and isinstance(entries[0], dict)
        else {}
    )


class SongStreamResolver:
    """Discover and select streams for one Song without an interface class.

    Reuses the Song's existing lazy webplayback and asset caches, and caches
    candidate discovery per client/wrapper. Refresh signed URLs with
    ``song.invalidate()`` when necessary.
    """

    def __init__(
        self,
        song: Song,
        *,
        wrapper_api: WrapperApi | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.song = song
        self.wrapper_api = wrapper_api
        self.transport = StreamTransport(client)
        self._client_key = id(client) if client else None
        self._wrapper_key = id(wrapper_api) if wrapper_api else None

    async def get_web_streams(self) -> tuple[WebPlaybackStream, ...]:
        """Discover direct playback flavors without requesting any playlists."""

        async def discover() -> tuple[WebPlaybackStream, ...]:
            record = _first_song(await self.song._get_webplayback())
            assets = record.get("assets")
            if not isinstance(assets, list):
                return ()
            result: list[WebPlaybackStream] = []
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                url = asset.get("URL")
                if not isinstance(url, str) or not url:
                    continue
                if self.song.is_library:
                    if result:
                        break
                    result.append(
                        WebPlaybackStream(
                            codec=SongCodec.AAC_WEB,
                            origin=StreamOrigin.LIBRARY_WEBPLAYBACK,
                            url=url,
                            transport=self.transport,
                            drm_free=True,
                        )
                    )
                    continue
                codec = next(
                    (
                        kind
                        for kind, flavor in WEB_FLAVORS.items()
                        if flavor == asset.get("flavor")
                    ),
                    None,
                )
                if codec is None:
                    continue
                bitrate = 256_000 if codec == SongCodec.AAC_WEB else 64_000
                result.append(
                    WebPlaybackStream(
                        codec=codec,
                        origin=StreamOrigin.WEBPLAYBACK,
                        url=url,
                        bitrate=bitrate,
                        transport=self.transport,
                    )
                )
            return tuple(result)

        return await self.song._load(("streams_web", self._client_key), discover)

    async def _primary_hls_source(self) -> tuple[str, StreamOrigin] | None:
        if self.wrapper_api is not None:

            async def playback() -> dict:
                return await self.wrapper_api.get_playback(self.song.id)

            try:
                wrapped = await self.song._load(
                    ("streams_wrapper", self._wrapper_key), playback
                )
            except GamdlApiError:
                # An unavailable optional wrapper must not prevent Asset HLS.
                wrapped = {}
            url = _first_song(wrapped).get("hls-playlist-url")
            if isinstance(url, str) and url:
                return _default_master_url(url), StreamOrigin.WRAPPER_HLS

        playback = await self.song._get_webplayback()
        url = _first_song(playback).get("hls-playlist-url")
        if isinstance(url, str) and url:
            return _default_master_url(url), StreamOrigin.WEBPLAYBACK_HLS
        return None

    async def _asset_hls_source(self) -> tuple[str, StreamOrigin] | None:
        if self.song.is_library:
            return None
        assets = await self.song._get_assets()
        entries = assets.get("results", {}).get("assets", [])
        if not isinstance(entries, list):
            return None
        for asset in entries:
            url = asset.get("url") if isinstance(asset, dict) else None
            if isinstance(url, str) and url:
                return _default_master_url(url), StreamOrigin.ASSET_HLS
        return None

    async def _from_hls_master(
        self, source: tuple[str, StreamOrigin]
    ) -> tuple[HlsSongStream, ...]:
        url, origin = source
        manifest = await self.transport.get_manifest(url)
        result: list[HlsSongStream] = []
        for playlist in manifest.playlists:
            info = playlist.stream_info
            group = getattr(info, "audio", None)
            codecs = getattr(info, "codecs", None)
            codec = match_hls_codec(group, codecs)
            if codec is None:
                continue
            bitrate = getattr(info, "average_bandwidth", None) or getattr(
                info, "bandwidth", None
            )
            result.append(
                HlsSongStream(
                    codec=codec,
                    origin=origin,
                    url=playlist.absolute_uri,
                    transport=self.transport,
                    codec_string=codecs,
                    bitrate=bitrate,
                )
            )
        return tuple(result)

    async def get_hls_streams(
        self, *, asset_fallback: bool = False
    ) -> tuple[HlsSongStream, ...]:
        """Discover HLS variants; optionally force the assets endpoint fallback."""
        key = (
            "streams_hls_asset" if asset_fallback else "streams_hls_primary",
            self._wrapper_key,
            self._client_key,
        )

        async def discover() -> tuple[HlsSongStream, ...]:
            if self.song.is_library:
                return ()
            primary = None if asset_fallback else await self._primary_hls_source()
            if primary is not None:
                try:
                    options = await self._from_hls_master(primary)
                except StreamManifestError:
                    options = ()
                if options:
                    return options
            asset = await self._asset_hls_source()
            if asset is None or (primary is not None and asset[0] == primary[0]):
                return ()
            try:
                return await self._from_hls_master(asset)
            except StreamManifestError:
                return ()

        from .exceptions import StreamManifestError

        return await self.song._load(key, discover)

    async def get_streams(self) -> tuple[AudioStream, ...]:
        """Return Webplayback plus available HLS candidates, unselected."""
        web = await self.get_web_streams()
        hls = await self.get_hls_streams()
        return (*web, *hls)

    async def get_stream(
        self,
        *,
        codec: SongCodec | None = None,
        codec_priority: Sequence[SongCodec] | None = None,
        selector: StreamSelector | None = None,
    ) -> AudioStream:
        """Pick best matching bitrate or request a choice for ASK.

        ``codec`` and ``codec_priority`` are mutually exclusive; the default
        is AAC Webplayback. The callback only runs if ASK is actually reached.
        """
        if codec is not None and codec_priority is not None:
            raise ValueError("Provide either codec or codec_priority, not both")
        priorities = (
            (SongCodec(codec),)
            if codec is not None
            else tuple(SongCodec(item) for item in codec_priority)
            if codec_priority is not None
            else (SongCodec.AAC_WEB,)
        )
        if not priorities:
            raise ValueError("codec_priority must not be empty")
        for preferred in priorities:
            if preferred == SongCodec.ASK:
                if selector is None:
                    raise SelectionRequiredError("song_stream")
                candidates = await self.get_streams()
                if not candidates:
                    raise StreamNotAvailableError("No selectable song streams")
                request = StreamSelectionRequest(
                    media_id=self.song.id,
                    options=tuple(
                        StreamSelectionOption(index=i, stream=item)
                        for i, item in enumerate(candidates)
                    ),
                )
                selection = selector(request)
                if inspect.isawaitable(selection):
                    selection = await selection
                if type(selection) is not int or not 0 <= selection < len(candidates):
                    raise ValueError("Stream selector returned an invalid index")
                return candidates[selection]
            if preferred.is_web:
                matches = [
                    item
                    for item in await self.get_web_streams()
                    if item.codec == preferred
                ]
            else:
                matches = [
                    item
                    for item in await self.get_hls_streams()
                    if item.codec == preferred
                ]
                if not matches:
                    matches = [
                        item
                        for item in await self.get_hls_streams(asset_fallback=True)
                        if item.codec == preferred
                    ]
            if matches:
                return max(matches, key=lambda item: item.bitrate or 0)
        raise StreamNotAvailableError(
            "No stream matches the requested song codec preferences"
        )
