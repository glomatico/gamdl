"""Typed interactive music-video video and audio selection requests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..stream import AudioStream, VideoStream
from ..song.enums import SongCodec
from .enums import MusicVideoCodec


@dataclass(frozen=True)
class VideoSelectionOption:
    index: int
    stream: VideoStream

    @property
    def codec(self) -> MusicVideoCodec:
        return self.stream.codec

    @property
    def width(self) -> int | None:
        return self.stream.width

    @property
    def height(self) -> int | None:
        return self.stream.height

    @property
    def frame_rate(self) -> float | None:
        return self.stream.frame_rate

    @property
    def bitrate(self) -> int | None:
        return self.stream.bitrate


@dataclass(frozen=True)
class VideoSelectionRequest:
    media_id: str
    options: tuple[VideoSelectionOption, ...]


@dataclass(frozen=True)
class AudioSelectionOption:
    index: int
    stream: AudioStream
    name: str | None = None
    language: str | None = None
    group_id: str | None = None

    @property
    def codec(self) -> SongCodec:
        return self.stream.codec

    @property
    def bitrate(self) -> int | None:
        return self.stream.bitrate


@dataclass(frozen=True)
class AudioSelectionRequest:
    media_id: str
    video: VideoStream
    options: tuple[AudioSelectionOption, ...]


MusicVideoStreamSelector = Callable[
    [VideoSelectionRequest | AudioSelectionRequest], int | Awaitable[int]
]
