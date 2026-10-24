"""Download selected HLS streams using Rust, exposed as an async iterator.

Playlist discovery remains in gamdl.media. Network segment fetching, bounded
concurrency and retries are implemented by the native Tokio/reqwest backend.
Without a decryption key, this layer yields potentially encrypted HLS bytes.
With a 16-byte AES key, Rust decrypts CENC/AES-CTR or CBCS/AES-CBC samples
before yielding or saving, preserving the fragmented MP4 layout.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from os import PathLike
from pathlib import Path
from tempfile import NamedTemporaryFile

from gamdl import _native
from gamdl.drm.models import ContentKey
from gamdl.media import HlsStream
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


class Stream(AsyncIterator[bytes]):
    """Async context manager yielding raw HLS bytes in playlist order.

    Stream owns the native HTTP client and cancels pending fragment requests
    when the context exits. Pass decryption_key as a ContentKey (preferred,
    verifies the key ID) or 16-byte AES key to decrypt CENC/AES-CTR and
    CBCS/AES-CBC audio or video tracks. Multi-key tracks and muxing are
    not supported yet.
    Do not share one Stream iterator between independent consumers.
    """

    def __init__(
        self,
        source: HlsStream,
        *,
        concurrency: int = 8,
        chunk_size: int = 262144,
        retries: int = 3,
        decryption_key: bytes | ContentKey | None = None,
    ) -> None:
        if not isinstance(source, HlsStream):
            raise TypeError("source must be an HlsStream")
        self.source = source
        self.concurrency = concurrency
        self.chunk_size = chunk_size
        self.retries = retries
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
        self._used = False

    async def __aenter__(self) -> Stream:
        if self._used:
            raise RuntimeError("A Stream instance cannot be reopened")
        self._used = True
        try:
            parts = await _plan(self.source)
            self._engine = _native.NativeStream(
                parts,
                self.concurrency,
                self.chunk_size,
                self.retries,
                self._key,
                self._kid,
            )
        finally:
            # The worker now owns the native copy (or validation has failed).
            self._key = None
            self._kid = None
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._engine is not None:
            self._final_progress = self.progress
            self._engine.close()
            self._engine = None

    @property
    def progress(self) -> StreamProgress:
        """Read a native progress snapshot without contacting the network."""
        if self._engine is not None:
            return StreamProgress(*self._engine.progress())
        if self._final_progress is not None:
            return self._final_progress
        raise RuntimeError("Stream progress is available after entering the context")

    def __aiter__(self) -> Stream:
        return self

    async def __anext__(self) -> bytes:
        if self._engine is None:
            raise RuntimeError("Stream must be used inside an async with block")
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
