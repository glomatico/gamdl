from enum import Enum

from .constants import (
    ARTIST_DOWNLOAD_SELECTION_KEY_MAP,
    ARTIST_DOWNLOAD_SELECTION_STR_MAP,
)


class DownloadMode(Enum):
    YTDLP = "ytdlp"
    NM3U8DLRE = "nm3u8dlre"


class RemuxMode(Enum):
    FFMPEG = "ffmpeg"
    MP4BOX = "mp4box"


class RemuxFormatMusicVideo(Enum):
    M4V = "m4v"
    MP4 = "mp4"


class ArtistDownloadSelection(Enum):
    MAIN_ALBUMS = "main-albums"
    COMPILATION_ALBUMS = "compilation-albums"
    LIVE_ALBUMS = "live-albums"
    SINGLES_EPS = "singles-eps"
    ALL_ALBUMS = "all-albums"
    TOP_SONGS = "top-songs"
    MUSIC_VIDEOS = "music-videos"

    @property
    def path_key(self) -> tuple[str, str]:
        return ARTIST_DOWNLOAD_SELECTION_KEY_MAP[self.value]

    def __str__(self) -> str:
        return ARTIST_DOWNLOAD_SELECTION_STR_MAP[self.value]
