"""UI-neutral lifecycle events for download clients (terminal, bots, etc.)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Literal

from gamdl.stream import StreamProgress

if TYPE_CHECKING:
    from .models import DownloadResult


class DownloadStage(str, Enum):
    FETCHING_TAGS = "fetching_tags"
    FETCHING_STREAMS = "fetching_streams"
    FETCHING_DECRYPTION_KEYS = "fetching_decryption_keys"
    DOWNLOADING = "downloading"
    MUXING = "muxing"
    TAGGING = "tagging"
    PUBLISHING = "publishing"
    SAVING_COVER = "saving_cover"
    SAVING_LYRICS = "saving_lyrics"
    UPDATING_DATABASE = "updating_database"
    UPDATING_PLAYLIST = "updating_playlist"
    COMPLETED = "completed"


class EventState(str, Enum):
    STARTED = "started"
    PROGRESS = "progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class DownloadEvent:
    media_id: str
    stage: DownloadStage
    state: EventState
    track: Literal["audio", "video"] | None = None
    progress: StreamProgress | None = None
    result: DownloadResult | None = None


DownloadEventCallback = Callable[[DownloadEvent], None]
