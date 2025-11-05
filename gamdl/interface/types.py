import datetime
from dataclasses import dataclass

from .enums import MediaFileFormat, MediaRating, MediaType


@dataclass
class Lyrics:
    synced: str = None
    unsynced: str = None


@dataclass
class MediaTags:
    album: str = None
    album_artist: str = None
    album_id: int = None
    album_sort: str = None
    artist: str = None
    artist_id: int = None
    artist_sort: str = None
    comment: str = None
    compilation: bool = None
    composer: str = None
    composer_id: int = None
    composer_sort: str = None
    copyright: str = None
    date: datetime.date | str = None
    disc: int = None
    disc_total: int = None
    gapless: bool = None
    genre: str = None
    genre_id: int = None
    lyrics: str = None
    media_type: MediaType = None
    rating: MediaRating = None
    storefront: str = None
    title: str = None
    title_id: int = None
    title_sort: str = None
    track: int = None
    track_total: int = None
    xid: str = None

    def as_mp4_tags(self, date_format: str = None) -> dict:
        disc_mp4 = [
            self.disc if self.disc is not None else 0,
            self.disc_total if self.disc_total is not None else 0,
        ]
        if disc_mp4[0] == 0 and disc_mp4[1] == 0:
            disc_mp4 = None

        track_mp4 = [
            self.track if self.track is not None else 0,
            self.track_total if self.track_total is not None else 0,
        ]
        if track_mp4[0] == 0 and track_mp4[1] == 0:
            track_mp4 = None

        if isinstance(self.date, datetime.date):
            if date_format is None:
                date_mp4 = self.date.isoformat()
            else:
                date_mp4 = self.date.strftime(date_format)
        elif isinstance(self.date, str):
            date_mp4 = self.date
        else:
            date_mp4 = None

        mp4_tags = {
            "\xa9alb": self.album,
            "aART": self.album_artist,
            "plID": self.album_id,
            "soal": self.album_sort,
            "\xa9ART": self.artist,
            "atID": self.artist_id,
            "soar": self.artist_sort,
            "\xa9cmt": self.comment,
            "cpil": bool(self.compilation) if self.compilation is not None else None,
            "\xa9wrt": self.composer,
            "cmID": self.composer_id,
            "soco": self.composer_sort,
            "cprt": self.copyright,
            "\xa9day": date_mp4,
            "disk": disc_mp4,
            "pgap": bool(self.gapless) if self.gapless is not None else None,
            "\xa9gen": self.genre,
            "\xa9lyr": self.lyrics,
            "geID": self.genre_id,
            "stik": int(self.media_type) if self.media_type is not None else None,
            "rtng": int(self.rating) if self.rating is not None else None,
            "sfID": self.storefront,
            "\xa9nam": self.title,
            "cnID": self.title_id,
            "sonm": self.title_sort,
            "trkn": track_mp4,
            "xid ": self.xid,
        }

        return {
            k: ([v] if not isinstance(v, bool) else v)
            for k, v in mp4_tags.items()
            if v is not None
        }


@dataclass
class PlaylistTags:
    playlist_artist: str = None
    playlist_id: int = None
    playlist_title: str = None
    playlist_track: int = None


@dataclass
class StreamInfo:
    stream_url: str = None
    widevine_pssh: str = None
    playready_pssh: str = None
    fairplay_key: str = None
    codec: str = None
    width: int = None
    height: int = None


@dataclass
class StreamInfoAv:
    media_id: str = None
    video_track: StreamInfo = None
    audio_track: StreamInfo = None
    file_format: MediaFileFormat = None


@dataclass
class DecryptionKey:
    kid: str = None
    key: str = None


@dataclass
class DecryptionKeyAv:
    video_track: DecryptionKey = None
    audio_track: DecryptionKey = None
