"""Reusable Apple Music artwork URL templates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .cover_cache import fetch_cover_bytes
from .exceptions import GamdlMediaError

if TYPE_CHECKING:
    from .cover_cache import CoverCache


@dataclass(frozen=True)
class Cover:
    """Artwork URL template with optional shared byte cache, loaded lazily."""

    url: str
    width: int | None = None
    height: int | None = None
    cache: CoverCache | None = field(default=None, compare=False, repr=False)

    @classmethod
    def from_metadata(
        cls, artwork: dict[str, Any], *, cache: CoverCache | None = None
    ) -> Cover:
        url = artwork.get("url")
        if not isinstance(url, str) or not url:
            raise GamdlMediaError("Artwork metadata contains no URL")
        return cls(
            url=url,
            width=artwork.get("width"),
            height=artwork.get("height"),
            cache=cache,
        )

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

    async def get_cover_bytes(
        self, *, width: int = 1200, height: int = 1200, format: str = "jpg"
    ) -> bytes | None:
        """Retrieve artwork bytes with the optional attached CoverCache."""
        url = self.get_formatted_url(width=width, height=height, format=format)
        if self.cache is not None:
            return await self.cache.get_bytes(url)
        return await fetch_cover_bytes(url)
