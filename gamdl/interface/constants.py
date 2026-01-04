MEDIA_TYPE_STR_MAP = {
    1: "Song",
    6: "Music Video",
}

MEDIA_RATING_STR_MAP = {
    0: "None",
    1: "Explicit",
    2: "Clean",
}

LEGACY_SONG_CODECS = {"aac-legacy", "aac-he-legacy"}

DRM_DEFAULT_KEY_MAPPING = {
    "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed": (
        "data:text/plain;base64,AAAAOHBzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAABgSEAAAAAA"
        "AAAAAczEvZTEgICBI88aJmwY="
    ),
    "com.microsoft.playready": (
        "data:text/plain;charset=UTF-16;base64,vgEAAAEAAQC0ATwAVwBSAE0ASABFAEEARABF"
        "AFIAIAB4AG0AbABuAHMAPQAiAGgAdAB0AHAAOgAvAC8AcwBjAGgAZQBtAGEAcwAuAG0AaQBjAH"
        "IAbwBzAG8AZgB0AC4AYwBvAG0ALwBEAFIATQAvADIAMAAwADcALwAwADMALwBQAGwAYQB5AFIA"
        "ZQBhAGQAeQBIAGUAYQBkAGUAcgAiACAAdgBlAHIAcwBpAG8AbgA9ACIANAAuADMALgAwAC4AMA"
        "AiAD4APABEAEEAVABBAD4APABQAFIATwBUAEUAQwBUAEkATgBGAE8APgA8AEsASQBEAFMAPgA8"
        "AEsASQBEACAAQQBMAEcASQBEAD0AIgBBAEUAUwBDAEIAQwAiACAAVgBBAEwAVQBFAD0AIgBBAE"
        "EAQQBBAEEAQQBBAEEAQQBBAEIAegBNAFMAOQBsAE0AUwBBAGcASQBBAD0APQAiAD4APAAvAEsA"
        "SQBEAD4APAAvAEsASQBEAFMAPgA8AC8AUABSAE8AVABFAEMAVABJAE4ARgBPAD4APAAvAEQAQQ"
        "BUAEEAPgA8AC8AVwBSAE0ASABFAEEARABFAFIAPgA="
    ),
    "com.apple.streamingkeydelivery": "skd://itunes.apple.com/P000000000/s1/e1",
}
MP4_FORMAT_CODECS = ["ec-3", "hvc1", "audio-atmos", "audio-ec3"]
SONG_CODEC_REGEX_MAP = {
    "aac": r"audio-stereo-\d+",
    "aac-he": r"audio-HE-stereo-\d+",
    "aac-binaural": r"audio-stereo-\d+-binaural",
    "aac-downmix": r"audio-stereo-\d+-downmix",
    "aac-he-binaural": r"audio-HE-stereo-\d+-binaural",
    "aac-he-downmix": r"audio-HE-stereo-\d+-downmix",
    "atmos": r"audio-atmos-.*",
    "ac3": r"audio-ac3-.*",
    "alac": r"audio-alac-.*",
}

FOURCC_MAP = {
    "h264": "avc1",
    "h265": "hvc1",
}

UPLOADED_VIDEO_QUALITY_RANK = [
    "1080pHdVideo",
    "720pHdVideo",
    "sdVideoWithPlusAudio",
    "sdVideo",
    "sd480pVideo",
    "provisionalUploadVideo",
]

IMAGE_FILE_EXTENSION_MAP = {
    "jpeg": ".jpg",
    "tiff": ".tif",
}
