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
    ATMOS = "atmos"
    AC3 = "ac3"
    ALAC = "alac"
    ASK = "ask"


class SyncedLyricsFormat(Enum):
    LRC = "lrc"
    SRT = "srt"
    TTML = "ttml"


class MusicVideoCodec(Enum):
    H264 = "h264"
    H265 = "h265"
    ASK = "ask"


class RemuxFormatMusicVideo(Enum):
    M4V = "m4v"
    MP4 = "mp4"


class MediaFileFormat(Enum):
    M4A = "m4a"
    MP4 = "mp4"
    M4V = "m4v"


class PostQuality(Enum):
    BEST = "best"
    ASK = "ask"


class CoverFormat(Enum):
    JPG = "jpg"
    PNG = "png"
    RAW = "raw"
