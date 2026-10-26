"""Typed two-stage source and variant selection requests for song streams."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..enums import HlsType, StreamOrigin
from ..stream import AudioStream
from .enums import SongCodec


@dataclass(frozen=True)
class StreamSourceOption:
    """One provider/profile, available before making a discovery request."""

    index: int
    origin: StreamOrigin


@dataclass(frozen=True)
class StreamSourceSelectionRequest:
    """First interactive prompt: choose a source before fetching streams."""

    media_id: str
    options: tuple[StreamSourceOption, ...]


@dataclass(frozen=True)
class StreamSelectionOption:
    """Second interactive prompt: choose from the selected source only."""

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

# Public name consistent with MusicVideoStreamSelector.
SongStreamSelector = StreamSelector
