"""Song-specific codec identifiers for streaming and selection."""

from __future__ import annotations

from enum import Enum


class SongCodec(str, Enum):
    """Actual song codec preferences; interactive selection is separate."""

    AAC_WEB = "aac-web"
    AAC_HE_WEB = "aac-he-web"
    AAC = "aac"
    AAC_HE = "aac-he"
    AAC_BINAURAL = "aac-binaural"
    AAC_DOWNMIX = "aac-downmix"
    AAC_HE_BINAURAL = "aac-he-binaural"
    AAC_HE_DOWNMIX = "aac-he-downmix"
    ATMOS = "atmos"
    AC3 = "ac3"
    ALAC = "alac"

    @property
    def is_web(self) -> bool:
        return self in {SongCodec.AAC_WEB, SongCodec.AAC_HE_WEB}
