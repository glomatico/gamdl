"""Stable opt-in live song for streaming, DRM, and future muxer tests.

The Brazilian storefront currently exposes every SongCodec except AC3 for
this catalog song. Assert actual availability in the live discovery tests;
the network catalog can change independently of the repository.
"""

from gamdl.media import SongCodec

SONG_ID = "1228739609"  # Dua Lipa — New Rules

WEB_CODECS = (SongCodec.AAC_WEB, SongCodec.AAC_HE_WEB)
ENHANCED_CODECS = (
    SongCodec.AAC,
    SongCodec.AAC_HE,
    SongCodec.AAC_BINAURAL,
    SongCodec.AAC_DOWNMIX,
    SongCodec.AAC_HE_BINAURAL,
    SongCodec.AAC_HE_DOWNMIX,
    SongCodec.ALAC_LOSSLESS,
    SongCodec.ALAC_HIRES,
    SongCodec.ATMOS,
)
STANDARD_CODECS = (SongCodec.AAC, SongCodec.AAC_HE)
# Apple currently rejects Widevine ALAC license requests for this fixture.
# The same ALAC rendition can be exercised through wrapper FairPlay delivery.
ENHANCED_WIDEVINE_CODECS = tuple(
    codec
    for codec in ENHANCED_CODECS
    if codec not in {SongCodec.ALAC_LOSSLESS, SongCodec.ALAC_HIRES}
)
