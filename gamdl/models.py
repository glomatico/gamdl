from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UrlInfo:
    storefront: str = None
    type: str = None
    id: str = None


@dataclass
class DownloadQueue:
    playlist_attributes: dict = None
    tracks_metadata: list[dict] = None


@dataclass
class Lyrics:
    synced: str = None
    unsynced: str = None


@dataclass
class StreamInfo:
    stream_url: str = None
    pssh: str = None
    codec: str = None
