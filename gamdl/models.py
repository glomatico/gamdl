from __future__ import annotations

from dataclasses import dataclass

from .enums import MediaFileFormat


@dataclass
class UrlInfo:
    storefront: str = None
    type: str = None
    id: str = None
    is_library: bool = None


@dataclass
class DownloadQueue:
    playlist_attributes: dict = None
    medias_metadata: list[dict] = None


@dataclass
class Lyrics:
    synced: str = None
    unsynced: str = None


@dataclass
class StreamInfo:
    stream_url: str = None
    widevine_pssh: str = None
    playready_pssh: str = None
    fairplay_key: str = None
    codec: str = None


@dataclass
class StreamInfoAv:
    video_track: StreamInfo = None
    audio_track: StreamInfo = None
    file_format: MediaFileFormat = None
