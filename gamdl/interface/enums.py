from enum import Enum

from .constants import (
    FOURCC_MAP,
    LEGACY_SONG_CODECS,
    MEDIA_RATING_STR_MAP,
    MEDIA_TYPE_STR_MAP,
)


class SyncedLyricsFormat(Enum):
    LRC = "lrc"
    SRT = "srt"
    TTML = "ttml"


class MediaType(Enum):
    SONG = 1
    MUSIC_VIDEO = 6

    def __str__(self) -> str:
        return MEDIA_TYPE_STR_MAP[self.value]

    def __int__(self) -> int:
        return self.value


class MediaRating(Enum):
    NONE = 0
    EXPLICIT = 1
    CLEAN = 2

    def __str__(self) -> str:
        return MEDIA_RATING_STR_MAP[self.value]

    def __int__(self) -> int:
        return self.value


class MediaFileFormat(Enum):
    MP4 = "mp4"
    M4V = "m4v"
    M4A = "m4a"


class SongCodec(Enum):
    AAC_LEGACY = "aac-legacy"
    AAC_HE_LEGACY = "aac-he-legacy"
    AAC = "aac"
    AAC_HE = "aac-he"
    AAC_BINAURAL = "aac-binaural"
    AAC_DOWNMIX = "aac-downmix"
    AAC_HE_BINAURAL = "aac-he-binaural"
    AAC_HE_DOWNMIX = "aac-he-downmix"
    ATMOS = "atmos"
    AC3 = "ac3"
    ALAC = "alac"
    ASK = "ask"

    def is_legacy(self) -> bool:
        return self.value in LEGACY_SONG_CODECS


class MusicVideoCodec(Enum):
    H264 = "h264"
    H265 = "h265"
    ASK = "ask"

    def fourcc(self) -> str:
        return FOURCC_MAP[self.value]


class MusicVideoResolution(Enum):
    R240P = "240p"
    R360P = "360p"
    R480P = "480p"
    R540P = "540p"
    R720P = "720p"
    R1080P = "1080p"
    R1440P = "1440p"
    R2160P = "2160p"

    def __int__(self) -> int:
        return int(self.value[:-1])


class UploadedVideoQuality(Enum):
    BEST = "best"
    ASK = "ask"


class CoverFormat(Enum):
    JPG = "jpg"
    PNG = "png"
    RAW = "raw"
