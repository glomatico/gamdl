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
from .exceptions import (
    SelectionRequiredError,
    StreamManifestError,
    StreamNotAvailableError,
)
from .stream import (
    WEB_FLAVORS,
    AudioStream,
    HlsType,
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
class StreamSourceOption:
    """One provider/profile, available before making a discovery request."""

    index: int
    origin: StreamOrigin


@dataclass(frozen=True)
class StreamSourceSelectionRequest:
    """First ASK prompt: choose a source before fetching any streams."""

    media_id: str
    options: tuple[StreamSourceOption, ...]


@dataclass(frozen=True)
class StreamSelectionOption:
    """Second ASK prompt: choose a variant from the chosen source only."""

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

    @property
    def hls_type(self) -> HlsType | None:
        return getattr(self.stream, "hls_type", None)


@dataclass(frozen=True)
class StreamSelectionRequest:
    """Candidates for a single selected provider/profile."""

    media_id: str
    origin: StreamOrigin
    options: tuple[StreamSelectionOption, ...]


StreamSelector = Callable[
    [StreamSourceSelectionRequest | StreamSelectionRequest], int | Awaitable[int]
]


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

    async def _wrapper_hls_source(self) -> tuple[str, StreamOrigin] | None:
        if self.wrapper_api is None:
            return None

        async def playback() -> dict:
            return await self.wrapper_api.get_playback(self.song.id)

        try:
            wrapped = await self.song._load(
                ("streams_wrapper", self._wrapper_key), playback
            )
        except GamdlApiError:
            # A missing/unavailable wrapper HLS should not prevent assets.
            return None
        url = _first_song(wrapped).get("hls-playlist-url")
        if isinstance(url, str) and url:
            return _default_master_url(url), StreamOrigin.WRAPPER_HLS
        return None

    async def _asset_hls_source(
        self, origin: StreamOrigin
    ) -> tuple[str, StreamOrigin] | None:
        if self.song.is_library:
            return None
        if origin not in {
            StreamOrigin.ASSET_ENHANCED_HLS,
            StreamOrigin.ASSET_STANDARD_HLS,
        }:
            raise ValueError("Expected an Assets API HLS source")
        profile = "enhancedHls" if origin == StreamOrigin.ASSET_ENHANCED_HLS else ""
        assets = await self.song._get_assets(hls_profile=profile)
        entries = assets.get("results", {}).get("assets", [])
        if not isinstance(entries, list):
            return None
        for asset in entries:
            url = asset.get("url") if isinstance(asset, dict) else None
            if isinstance(url, str) and url:
                return _default_master_url(url), origin
        return None

    async def _from_hls_master(
        self, source: tuple[str, StreamOrigin]
    ) -> tuple[HlsSongStream, ...]:
        url, origin = source
        manifest = await self.transport.get_manifest(url)
        hls_type = (
            HlsType.ENHANCED
            if any(getattr(p.stream_info, "audio", None) for p in manifest.playlists)
            else HlsType.STANDARD
        )
        result: list[HlsSongStream] = []
        for playlist in manifest.playlists:
            info = playlist.stream_info
            group = getattr(info, "audio", None)
            codecs = getattr(info, "codecs", None)
            codec = match_hls_codec(group, codecs)
            if codec is None:
                continue
            if hls_type == HlsType.STANDARD and codec not in {
                SongCodec.AAC,
                SongCodec.AAC_HE,
            }:
                continue
            bitrate = getattr(info, "average_bandwidth", None) or getattr(
                info, "bandwidth", None
            )
            result.append(
                HlsSongStream(
                    codec=codec,
                    origin=origin,
                    hls_type=hls_type,
                    url=playlist.absolute_uri,
                    transport=self.transport,
                    codec_string=codecs,
                    bitrate=bitrate,
                )
            )
        return tuple(result)

    async def get_hls_streams(
        self, *, origin: StreamOrigin
    ) -> tuple[HlsSongStream, ...]:
        """Discover one explicit HLS source, without implicit API fallbacks.

        The ``enhancedHls`` *request* can return standard HLS on Apple's side;
        the actual type is detected from the master, not inferred from origin.
        """
        if origin not in {
            StreamOrigin.WRAPPER_HLS,
            StreamOrigin.ASSET_ENHANCED_HLS,
            StreamOrigin.ASSET_STANDARD_HLS,
        }:
            raise ValueError("Unsupported HLS source")
        key = (
            "streams_hls",
            origin,
            self._wrapper_key if origin == StreamOrigin.WRAPPER_HLS else None,
            self._client_key,
        )

        async def discover() -> tuple[HlsSongStream, ...]:
            if self.song.is_library:
                return ()
            source = (
                await self._wrapper_hls_source()
                if origin == StreamOrigin.WRAPPER_HLS
                else await self._asset_hls_source(origin)
            )
            if source is None:
                return ()
            try:
                return await self._from_hls_master(source)
            except StreamManifestError:
                return ()

        return await self.song._load(key, discover)

    async def get_streams(self) -> tuple[AudioStream, ...]:
        """Explicit, full discovery of all eligible sources, including both profiles."""
        candidates: list[AudioStream] = list(await self.get_web_streams())
        if not self.song.is_library:
            for origin in self._hls_origins():
                candidates.extend(await self.get_hls_streams(origin=origin))
        return tuple(candidates)

    def _hls_origins(self) -> tuple[StreamOrigin, ...]:
        return (
            *((StreamOrigin.WRAPPER_HLS,) if self.wrapper_api is not None else ()),
            StreamOrigin.ASSET_ENHANCED_HLS,
            StreamOrigin.ASSET_STANDARD_HLS,
        )

    def _source_options(self) -> tuple[StreamSourceOption, ...]:
        origins = [
            StreamOrigin.LIBRARY_WEBPLAYBACK
            if self.song.is_library
            else StreamOrigin.WEBPLAYBACK
        ]
        if not self.song.is_library:
            origins.extend(self._hls_origins())
        return tuple(StreamSourceOption(i, origin) for i, origin in enumerate(origins))

    async def _streams_for_source(
        self, origin: StreamOrigin
    ) -> tuple[AudioStream, ...]:
        if origin in {StreamOrigin.WEBPLAYBACK, StreamOrigin.LIBRARY_WEBPLAYBACK}:
            return await self.get_web_streams()
        return await self.get_hls_streams(origin=origin)

    @staticmethod
    async def _select_index(
        selector: StreamSelector,
        request: StreamSourceSelectionRequest | StreamSelectionRequest,
        count: int,
    ) -> int:
        choice = selector(request)
        if inspect.isawaitable(choice):
            choice = await choice
        if type(choice) is not int or not 0 <= choice < count:
            raise ValueError("Stream selector returned an invalid index")
        return choice

    async def _ask(self, selector: StreamSelector) -> AudioStream:
        sources = self._source_options()
        source_request = StreamSourceSelectionRequest(self.song.id, sources)
        source_index = await self._select_index(selector, source_request, len(sources))
        origin = sources[source_index].origin
        candidates = await self._streams_for_source(origin)
        if not candidates:
            raise StreamNotAvailableError(
                "No song streams available from selected source"
            )
        request = StreamSelectionRequest(
            media_id=self.song.id,
            origin=origin,
            options=tuple(
                StreamSelectionOption(i, stream) for i, stream in enumerate(candidates)
            ),
        )
        chosen = await self._select_index(selector, request, len(candidates))
        return candidates[chosen]

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
                return await self._ask(selector)
            if preferred.is_web:
                matches = [
                    item
                    for item in await self.get_web_streams()
                    if item.codec == preferred
                ]
            else:
                # Match codecs independently within each source. Wrapper
                # always wins over assets, even when its bitrate is lower.
                origins = (
                    (StreamOrigin.WRAPPER_HLS, StreamOrigin.ASSET_ENHANCED_HLS)
                    if self.wrapper_api is not None
                    else (StreamOrigin.ASSET_ENHANCED_HLS,)
                )
                for origin in origins:
                    matches = [
                        item
                        for item in await self.get_hls_streams(origin=origin)
                        if item.codec == preferred
                    ]
                    if matches:
                        return max(matches, key=lambda item: item.bitrate or 0)
                continue
            if matches:
                return max(matches, key=lambda item: item.bitrate or 0)
        raise StreamNotAvailableError(
            "No stream matches the requested song codec preferences"
        )
