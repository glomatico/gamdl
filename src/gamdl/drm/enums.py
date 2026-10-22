"""Supported content-decryption backends."""

from enum import Enum


class DrmBackend(str, Enum):
    WIDEVINE = "widevine"
    PLAYREADY = "playready"

    @property
    def key_system(self) -> str:
        return {
            DrmBackend.WIDEVINE: "com.widevine.alpha",
            DrmBackend.PLAYREADY: "com.microsoft.playready",
        }[self]
