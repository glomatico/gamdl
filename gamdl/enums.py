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

    def is_legacy(self) -> bool:
        return self in {SongCodec.AAC_LEGACY, SongCodec.AAC_HE_LEGACY}


class SyncedLyricsFormat(Enum):
    LRC = "lrc"
    SRT = "srt"
    TTML = "ttml"


class MusicVideoCodec(Enum):
    H264 = "h264"
    H265 = "h265"
    ASK = "ask"

    def fourcc(self) -> str:
        return {
            MusicVideoCodec.H264: "avc1",
            MusicVideoCodec.H265: "hvc1",
        }.get(self)


class RemuxFormatMusicVideo(Enum):
    M4V = "m4v"
    MP4 = "mp4"


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


class MediaType(Enum):
    SONG = 1
    MUSIC_VIDEO = 6

    def __str__(self) -> str:
        return {
            MediaType.SONG: "Song",
            MediaType.MUSIC_VIDEO: "Music Video",
        }[self]

    def __int__(self) -> int:
        return self.value


class MediaRating(Enum):
    NONE = 0
    EXPLICIT = 1
    CLEAN = 2

    def __str__(self) -> str:
        return {
            MediaRating.NONE: "None",
            MediaRating.EXPLICIT: "Explicit",
            MediaRating.CLEAN: "Clean",
        }[self]

    def __int__(self) -> int:
        return self.value
