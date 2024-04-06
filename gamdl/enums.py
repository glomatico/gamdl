from enums import Enums


class DownloadMode(Enums):
    YTDLP = "ytdlp"
    NM3U8DLRE = "nm3u8dlre"


class SongCodec(Enums):
    AAC_LEGACY = "aac-legacy"
    AAC_HE_LEGACY = "aac-he-legacy"
    AAC_BINAURAL = "aac-binaural"
    AAC_DOWNMIX = "aac-downmix"
    ALAC = "alac"
    ATMOS = "atmos"


class ArtworkFormat(Enums):
    JPG = "jpg"
    PNG = "png"
