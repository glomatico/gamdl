"""Reusable Apple Music artwork URL templates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .exceptions import GamdlMediaError


@dataclass(frozen=True)
class Cover:
    """Artwork URL template and optional dimensions; no network side effects."""

    url: str
    width: int | None = None
    height: int | None = None

    @classmethod
    def from_metadata(cls, artwork: dict[str, Any]) -> Cover:
        url = artwork.get("url")
        if not isinstance(url, str) or not url:
            raise GamdlMediaError("Artwork metadata contains no URL")
        return cls(url=url, width=artwork.get("width"), height=artwork.get("height"))

    def get_raw_url(self) -> str:
        """Return the original URL or Apple artwork template unchanged."""
        return self.url

    def get_formatted_url(self, *, width: int, height: int, format: str = "jpg") -> str:
        """Expand Apple's width/height/format placeholders without HTTP IO."""
        if width <= 0 or height <= 0:
            raise ValueError("Artwork dimensions must be positive")
        if format not in {"jpg", "jpeg", "png", "webp"}:
            raise ValueError("Unsupported artwork format")
        return (
            self.url.replace("{w}", str(width))
            .replace("{h}", str(height))
            .replace("{f}", format)
        )
