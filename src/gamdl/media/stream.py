"""Lazy audio/video descriptors for HLS playlists and progressive HTTPS files.

This layer discovers and describes streams. It does not acquire licenses,
extract decryption keys, download full media, or write output files.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import m3u8

from gamdl.drm.models import DRMInfo
from gamdl.transport import PublicTransport, PublicTransportError

from .cache import InstanceCache
from .enums import HlsType, StreamOrigin, StreamSource
from .exceptions import StreamManifestError
from .models import StreamSegment

if TYPE_CHECKING:
    from m3u8.model import M3U8

    from .song.enums import SongCodec


class StreamTransport(PublicTransport):
    """HLS resource parsing on top of the shared public HTTP transport."""

    async def get(self, url: str, *, byte_range: str | None = None) -> bytes:
        try:
            result = await self.fetch(url, byte_range=byte_range)
            assert result is not None
            return result
        except PublicTransportError as error:
            message = {
                "invalid_url": "Expected a valid HTTPS streaming URL",
                "missing_redirect": "CDN redirect had no destination",
                "too_many_redirects": "Too many CDN redirects",
            }.get(error.reason, "Unable to retrieve streaming resource")
            raise StreamManifestError(message) from None

    async def get_manifest(self, url: str) -> M3U8:
        data = await self.get(url)
        try:
            content = data.decode("utf-8-sig")
            if not content.lstrip().startswith("#EXTM3U"):
                raise ValueError
            return m3u8.loads(content, uri=url)
        except (UnicodeError, ValueError) as exc:
            raise StreamManifestError("Invalid HLS playlist data") from exc


class HttpStream:
    """One progressive HTTP media file, not an HLS playlist.

    No license metadata is extracted from standalone MP4/MP3 resources. The
    progressive downloader preserves file bytes as-is, including encryption.
    The stream origin identifies the provider, not the transfer protocol.
    """

    def __init__(
        self,
        url: str,
        *,
        codec: SongCodec | str | None = None,
        origin: StreamOrigin = StreamOrigin.WEBPLAYBACK,
        bitrate: int | None = None,
        media_id: str | None = None,
    ) -> None:
        self.url = url
        self.codec = codec
        self.origin = origin
        self.bitrate = bitrate
        self.media_id = media_id

    async def get_drm_info(self) -> DRMInfo:
        """No playlist-side DRM signaling is available for direct files."""
        return DRMInfo()


class HlsStream:
    """One selected HLS media track; manifest and DRM load on demand."""

    def __init__(
        self,
        *,
        codec: SongCodec | str,
        origin: StreamOrigin,
        url: str,
        transport: StreamTransport,
        bitrate: int | None = None,
        codec_string: str | None = None,
        drm_free: bool = False,
        media_id: str | None = None,
    ) -> None:
        self.codec = codec
        self.origin = origin
        self.url = url
        self.bitrate = bitrate
        self.codec_string = codec_string
        self.drm_free = drm_free
        self.media_id = media_id
        self._transport = transport
        self._memo = InstanceCache()

    @property
    def source_api(self) -> StreamSource:
        """Identify the URL provider separately from HLS playlist structure."""
        if self.origin == StreamOrigin.WRAPPER_HLS:
            return StreamSource.WRAPPER
        if self.origin == StreamOrigin.ITUNES_HLS:
            return StreamSource.ITUNES
        if self.origin in {
            StreamOrigin.ASSET_ENHANCED_HLS,
            StreamOrigin.ASSET_STANDARD_HLS,
        }:
            return StreamSource.ASSETS
        return StreamSource.WEBPLAYBACK

    async def _load(self, name: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        return await self._memo.get(name, factory)

    async def get_manifest(self) -> M3U8:
        """Fetch the *selected media playlist*, never a full media segment."""
        return await self._load(
            "manifest", lambda: self._transport.get_manifest(self.url)
        )

    async def get_segments(self) -> tuple[StreamSegment, ...]:
        """Describe media segments without downloading them."""

        async def load() -> tuple[StreamSegment, ...]:
            manifest = await self.get_manifest()
            return tuple(
                StreamSegment(seg.absolute_uri, seg.duration, seg.byterange)
                for seg in manifest.segments
            )

        return await self._load("segments", load)

    async def get_init_segment(self) -> bytes | None:
        """Fetch an HLS EXT-X-MAP initialization segment if one exists."""

        async def load() -> bytes | None:
            manifest = await self.get_manifest()
            segment = next((seg for seg in manifest.segments if seg.init_section), None)
            if segment is None:
                return None
            section = segment.init_section
            byte_range = None
            if section.byterange:
                # EXT-X-MAP BYTERANGE="length@offset"; HLS defaults offset
                # to zero here when no explicit offset is supplied.
                try:
                    parts = section.byterange.split("@", 1)
                    length = int(parts[0])
                    offset = int(parts[1]) if len(parts) == 2 else 0
                    if length <= 0 or offset < 0:
                        raise ValueError
                except ValueError as exc:
                    raise StreamManifestError(
                        "Invalid HLS init-section byte range"
                    ) from exc
                byte_range = f"{offset}-{offset + length - 1}"
            return await self._transport.get(
                section.absolute_uri, byte_range=byte_range
            )

        return await self._load("init_segment", load)

    async def get_drm_info(self) -> DRMInfo:
        """Inspect EXT-X-KEY/SESSION-KEY metadata with no license request."""

        async def load() -> DRMInfo:
            if self.drm_free:
                return DRMInfo()
            manifest = await self.get_manifest()
            keys = [key for key in manifest.keys if key is not None]
            widevine_pssh = playready_pssh = None
            widevine_uri = playready_uri = fairplay_uri = method = None
            untyped_data = untyped_uri = None
            for key in keys:
                method = method or key.method
                uri = key.uri
                if not isinstance(uri, str):
                    continue
                kind = (key.keyformat or "").lower()
                if kind == "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed":
                    widevine_pssh = _decode_data_uri(uri)
                    widevine_uri = uri
                elif kind == "com.microsoft.playready":
                    playready_pssh = _decode_data_uri(uri)
                    playready_uri = uri
                elif kind == "com.apple.streamingkeydelivery":
                    # Apple's universal placeholder is not a usable key URI.
                    if uri != "skd://itunes.apple.com/P000000000/s1/e1":
                        fairplay_uri = uri
                elif not kind or kind == "identity":
                    # Webplayback often provides a KEY with no KEYFORMAT.
                    # This isn't necessarily Widevine/PlayReady PSSH: keep
                    # the untyped data separately instead of mislabeling it.
                    untyped_data = _decode_data_uri(uri)
                    untyped_uri = uri
            return DRMInfo(
                widevine_pssh=widevine_pssh,
                playready_pssh=playready_pssh,
                fairplay_streaming=fairplay_uri,
                widevine_uri=widevine_uri,
                playready_uri=playready_uri,
                untyped_key_data=untyped_data,
                untyped_key_uri=untyped_uri,
                encryption_method=method,
            )

        return await self._load("drm", load)


class AudioStream(HlsStream):
    """Selected audio track, with the existing SongCodec-based interface."""


class VideoStream(HlsStream):
    """Selected video track with resolution and frame-rate metadata."""

    def __init__(
        self,
        *,
        width: int | None = None,
        height: int | None = None,
        frame_rate: float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.width = width
        self.height = height
        self.frame_rate = frame_rate


class WebPlaybackStream(AudioStream):
    """Direct web-playback media playlist, selected by asset flavor."""


class HlsSongStream(AudioStream):
    """Enhanced or standard HLS media playlist selected from a master."""

    def __init__(
        self,
        *,
        hls_type: HlsType = HlsType.STANDARD,
        audio_group: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.hls_type = hls_type
        self.audio_group = audio_group


def _decode_data_uri(uri: str) -> bytes | None:
    if not uri.startswith("data:") or ";base64," not in uri:
        return None
    try:
        return base64.b64decode(uri.split(",", 1)[1], validate=True)
    except (ValueError, base64.binascii.Error):
        return None
