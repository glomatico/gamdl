from enum import Enum


class DownloadMode(Enum):
    YTDLP = "ytdlp"
    NM3U8DLRE = "nm3u8dlre"


class RemuxMode(Enum):
    FFMPEG = "ffmpeg"
    MP4BOX = "mp4box"


class SongCodec(Enum):
    AAC_LEGACY = "aac-legacy"
    AAC_HE_LEGACY = "aac-he-legacy"
    AAC = "aac"
    AAC_HE = "aac-he"
    AAC_BINAURAL = "aac-binaural"
    AAC_DOWNMIX = "aac-downmix"
    AAC_HE_BINAURAL = "aac-he-binaural"
    AAC_HE_DOWNMIX = "aac-he-downmix"
    ALAC = "alac"
    ATMOS = "atmos"
    ASK = "ask"


class SyncedLyricsFormat(Enum):
    LRC = "lrc"
    SRT = "srt"
    TTML = "ttml"


class MusicVideoCodec(Enum):
    H264_BEST = "h264-best"
    H265_BEST = "h265-best"
    ASK = "ask"


class PostQuality(Enum):
    BEST = "best"
    ASK = "ask"


class CoverFormat(Enum):
    JPG = "jpg"
    PNG = "png"
