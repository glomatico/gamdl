"""Download individual HLS tracks or progressive HTTP files using Rust.

Playlist discovery remains in gamdl.media. Network requests, bounded
concurrency and retries are implemented by the native Tokio/reqwest backend.
This layer can automatically acquire a key through an injected DRM backend,
use wrapper-v2 for eligible Apple song HLS, or pass through encrypted bytes if
neither method is available. Rust decrypts CENC/AES-CTR and CBCS/AES-CBC
samples before yielding or saving, preserving the fragmented MP4 layout.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from os import PathLike
from pathlib import Path
from tempfile import NamedTemporaryFile

from gamdl import _native
from gamdl.api.wrapper import WrapperApi
from gamdl.drm import Drm, DrmLicenseError
from gamdl.drm.models import ContentKey
from gamdl.media import (
    AudioStream,
    HlsStream,
    HlsSongStream,
    HttpStream,
    HlsType,
    StreamOrigin,
)
from gamdl.media.exceptions import StreamManifestError
from .progress import StreamProgress


def _range(raw: str | None, previous_end: int | None, *, init: bool = False):
    if not raw:
        return None, None, None
    try:
        parts = raw.split("@")
        if len(parts) > 2:
            raise ValueError
        length = int(parts[0])
        offset = int(parts[1]) if len(parts) == 2 else 0 if init else previous_end
        if length <= 0 or offset is None or offset < 0:
            raise ValueError
        end = offset + length - 1
        if end >= 2**64:
            raise ValueError
    except ValueError:
        raise StreamManifestError("Invalid HLS byte range") from None
    return offset, end, end + 1


async def _plan(source: HlsStream) -> list[tuple[str, int | None, int | None]]:
    manifest = await source.get_manifest()
    if not manifest.is_endlist:
        raise StreamManifestError("Only completed HLS media playlists are supported")
    if not manifest.segments:
        raise StreamManifestError("Selected HLS playlist contains no media segments")
    if any(key and key.method == "AES-128" for key in manifest.keys):
        raise StreamManifestError("AES-128 HLS transport decryption is not supported")
    parts: list[tuple[str, int | None, int | None]] = []
    current_map = None
    previous_uri = None
    previous_end = None
    for segment in manifest.segments:
        if getattr(segment, "discontinuity", False):
            raise StreamManifestError("HLS discontinuities are not supported")
        if getattr(segment, "gap_tag", False):
            raise StreamManifestError("HLS gap segments are not supported")
        section = segment.init_section
        if section is not None:
            init_start, init_end, _ = _range(section.byterange, None, init=True)
            new_map = (section.absolute_uri, init_start, init_end)
            if new_map != current_map:
                parts.append(new_map)
                current_map = new_map
        uri = segment.absolute_uri
        if not isinstance(uri, str) or not uri:
            raise StreamManifestError("Invalid HLS segment URL")
        start, end, previous_end = _range(
            segment.byterange,
            previous_end if previous_uri == uri else None,
        )
        previous_uri = uri
        parts.append((uri, start, end))
    return parts


class Streamer(AsyncIterator[bytes]):
    """Async context manager yielding bytes from one media track.

    Streamer owns the native HTTP client and cancels pending requests
    when the context exits. Wrapper-v2 is preferred when supplied for eligible
    song HLS with usable FairPlay metadata. Otherwise, an injected Drm backend
    acquires the content key when compatible signaling exists. With neither
    path available, stream bytes are passed through unchanged, even if encrypted.
    A manually supplied decryption_key overrides automatic selection and is
    mutually exclusive with drm. License/decryption failures are never silently
    downgraded to encrypted output. Enhanced HLS song CBCS automatically
    applies the legacy prefetch key to sample description 1 and the licensed
    key to description 2. Wrapper delivery is not supported for music videos.
    Progressive HTTP downloads are returned unchanged; MP4 DRM for standalone
    files, arbitrary HLS key rotation and track muxing are not supported yet.
    Do not share one Streamer iterator between independent consumers.
    """

    def __init__(
        self,
        source: HlsStream | HttpStream,
        *,
        concurrency: int = 8,
        chunk_size: int = 262144,
        retries: int = 3,
        decryption_key: bytes | ContentKey | None = None,
        wrapper_api: WrapperApi | None = None,
        drm: Drm | None = None,
    ) -> None:
        if not isinstance(source, (HlsStream, HttpStream)):
            raise TypeError("source must be an HlsStream or HttpStream")
        self.source = source
        self.concurrency = concurrency
        self.chunk_size = chunk_size
        self.retries = retries
        if wrapper_api is not None and not isinstance(wrapper_api, WrapperApi):
            raise TypeError("wrapper_api must be a WrapperApi")
        if drm is not None and not isinstance(drm, Drm):
            raise TypeError("drm must be a Drm")
        if drm is not None and decryption_key is not None:
            raise ValueError("Use either decryption_key or drm, not both")
        if isinstance(source, HttpStream) and decryption_key is not None:
            raise ValueError("Progressive HTTP MP4 decryption is not supported")
        self._drm = drm
        if (
            wrapper_api is not None
            and isinstance(source, HlsSongStream)
            and source.origin
            in {
                StreamOrigin.WRAPPER_HLS,
                StreamOrigin.ASSET_ENHANCED_HLS,
                StreamOrigin.ASSET_STANDARD_HLS,
            }
        ):
            self._wrapper = (
                wrapper_api.decrypt_host,
                wrapper_api.decrypt_port,
                source.media_id,
            )
        else:
            self._wrapper = None
        if isinstance(decryption_key, ContentKey):
            self._key = decryption_key.key
            self._kid = decryption_key.kid
        elif isinstance(decryption_key, bytes):
            self._key = decryption_key
            self._kid = None
        elif decryption_key is None:
            self._key = None
            self._kid = None
        else:
            raise TypeError("decryption_key must be bytes or a ContentKey")
        self._engine: _native.NativeStream | None = None
        self._final_progress: StreamProgress | None = None
        self._decryption_method: str | None = None
        self._used = False

    async def __aenter__(self) -> Streamer:
        if self._used:
            raise RuntimeError("A Streamer instance cannot be reopened")
        self._used = True
        try:
            if isinstance(self.source, HttpStream):
                self._engine = _native.NativeStream.progressive(
                    self.source.url, self.chunk_size, self.retries
                )
                self._decryption_method = "none"
                return self
            parts = await _plan(self.source)
            key, kid = self._key, self._kid
            method = "explicit_key" if key is not None else "none"
            wrapper = None
            if key is None and (self._wrapper is not None or self._drm is not None):
                info = await self.source.get_drm_info()
                fairplay_uri = info.fairplay_streaming
                if (
                    self._wrapper is not None
                    and isinstance(fairplay_uri, str)
                    and fairplay_uri.startswith("skd://")
                    and len(fairplay_uri) > len("skd://")
                ):
                    if not self.source.media_id:
                        raise ValueError(
                            "Wrapper sample delivery requires a song media ID"
                        )
                    wrapper = (*self._wrapper, fairplay_uri)
                    method = "wrapper"
                elif self._drm is not None and self._drm.has_signaling(info):
                    if not self.source.media_id:
                        raise ValueError("DRM license request requires a media ID")
                    content_key = await self._drm.get_keys(
                        media_id=self.source.media_id,
                        drm_info=info,
                        is_library=self.source.origin
                        == StreamOrigin.LIBRARY_WEBPLAYBACK,
                    )
                    if content_key is None:
                        raise DrmLicenseError("DRM license returned no content key")
                    key, kid = content_key.key, content_key.kid
                    method = self._drm.backend.value
            enhanced_song = (
                isinstance(self.source, HlsSongStream)
                and self.source.hls_type == HlsType.ENHANCED
            ) or (
                not isinstance(self.source, HlsSongStream)
                and isinstance(self.source, AudioStream)
                and self.source.origin == StreamOrigin.ASSET_ENHANCED_HLS
            )
            self._engine = _native.NativeStream(
                parts,
                self.concurrency,
                self.chunk_size,
                self.retries,
                key,
                kid,
                enhanced_song,
                wrapper,
                isinstance(self.source, HlsSongStream)
                and self.source.origin
                in {
                    StreamOrigin.ASSET_STANDARD_HLS,
                    StreamOrigin.ASSET_ENHANCED_HLS,
                },
            )
            self._decryption_method = method
        finally:
            # The worker now owns the native copy (or validation has failed).
            self._key = None
            self._kid = None
            self._wrapper = None
            self._drm = None
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._engine is not None:
            self._final_progress = self.progress
            self._engine.close()
            self._engine = None

    @property
    def decryption_method(self) -> str:
        """Selected decryption method (available after entering the context)."""
        if self._decryption_method is None:
            raise RuntimeError(
                "Decryption method is available after entering the context"
            )
        return self._decryption_method

    @property
    def progress(self) -> StreamProgress:
        """Read a native progress snapshot without contacting the network."""
        if self._engine is not None:
            return StreamProgress(*self._engine.progress())
        if self._final_progress is not None:
            return self._final_progress
        raise RuntimeError("Streamer progress is available after entering the context")

    def __aiter__(self) -> Streamer:
        return self

    async def __anext__(self) -> bytes:
        if self._engine is None:
            raise RuntimeError("Streamer must be used inside an async with block")
        result = await self._engine.next_chunk()
        if result is None:
            raise StopAsyncIteration
        return result

    async def read(self) -> bytes:
        """Collect all remaining chunks in memory; best for small media only."""
        return b"".join([chunk async for chunk in self])

    async def save(
        self,
        path: str | PathLike[str],
        *,
        progress_callback: Callable[[StreamProgress], None] | None = None,
    ) -> Path:
        """Save raw stream bytes, atomically publishing only on success."""
        destination = Path(path)
        tmp_path: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="wb",
                prefix=f".{destination.name}.",
                suffix=".part",
                dir=destination.parent,
                delete=False,
            ) as output:
                tmp_path = Path(output.name)
                async for chunk in self:
                    output.write(chunk)
                    if progress_callback is not None:
                        progress_callback(self.progress)
            tmp_path.replace(destination)
            return destination
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
