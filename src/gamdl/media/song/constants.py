"""Apple Music song codec and webplayback flavor mappings."""

from .enums import SongCodec

# Keep these aligned with the original Gamdl codec mapping. The AUDIO group
# in Apple's enhanced master playlist is not the RFC CODECS identifier.
SONG_CODEC_REGEX_MAP: dict[SongCodec, str] = {
    SongCodec.AAC: r"audio-stereo-\d+",
    SongCodec.AAC_HE: r"audio-HE-stereo-\d+",
    SongCodec.AAC_BINAURAL: r"audio-stereo-\d+-binaural",
    SongCodec.AAC_DOWNMIX: r"audio-stereo-\d+-downmix",
    SongCodec.AAC_HE_BINAURAL: r"audio-HE-stereo-\d+-binaural",
    SongCodec.AAC_HE_DOWNMIX: r"audio-HE-stereo-\d+-downmix",
    SongCodec.ATMOS: r"audio-atmos-.*",
    SongCodec.AC3: r"audio-ac3-.*",
}
WEB_FLAVORS: dict[SongCodec, str] = {
    SongCodec.AAC_WEB: "28:ctrp256",
    SongCodec.AAC_HE_WEB: "32:ctrp64",
}
