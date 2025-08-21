from __future__ import annotations

import typing
from dataclasses import dataclass

from .enums import MediaFileFormat, MediaType, MediaRating


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


@dataclass
class MediaTags:
    album: str = None
    album_artist: str = None
    album_id: str = None
    album_sort: str = None
    artist: str = None
    artist_id: str = None
    artist_sort: str = None
    comment: str = None
    compilation: bool = None
    composer: str = None
    composer_id: str = None
    composer_sort: str = None
    copyright: str = None
    date: str = None
    disc: int = None
    disc_total: int = None
    gapless: bool = None
    genre: str = None
    genre_id: str = None
    lyrics: str = None
    media_type: MediaType = None
    rating: MediaRating = None
    storefront: str = None
    title: str = None
    title_id: str = None
    title_sort: str = None
    track: int = None
    track_total: int = None
    xid: str = None

    def to_mp4_tags(self) -> dict[str, typing.Any]:
        disc_mp4 = [
            self.disc if self.disc is not None else 0,
            self.disc_total if self.disc is not None else 0,
        ]
        if disc_mp4[0] == 0 and disc_mp4[1] == 0:
            disc_mp4 = [None]

        track_mp4 = [
            self.track if self.track is not None else 0,
            self.track_total if self.track is not None else 0,
        ]
        if track_mp4[0] == 0 and track_mp4[1] == 0:
            track_mp4 = [None]

        return {
            "\xa9alb": [self.album],
            "aART": [self.album_artist],
            "plID": [self.album_id],
            "soal": [self.album_sort],
            "\xa9ART": [self.artist],
            "atID": [self.artist_id],
            "soar": [self.artist_sort],
            "\xa9cmt": [self.comment],
            "cpil": [int(self.compilation)],
            "\xa9wrt": [self.composer],
            "cmID": [self.composer_id],
            "soco": [self.composer_sort],
            "cprt": [self.copyright],
            "\xa9day": [self.date],
            "disk": disc_mp4,
            "pgap": [int(self.gapless)],
            "\xa9gen": [self.genre],
            "\xa9lyr": [self.lyrics],
            "geID": [self.genre_id],
            "stik": (
                [int(self.media_type)] if int(self.media_type) is not None else [None]
            ),
            "rtng": [int(self.rating)],
            "sfID": [self.storefront],
            "\xa9nam": [self.title],
            "cnID": [self.title_id],
            "sonm": [self.title_sort],
            "xid ": [self.xid],
        }
