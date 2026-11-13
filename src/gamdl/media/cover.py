"""Reusable Apple Music artwork URL templates."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from .cover_cache import fetch_cover_bytes
from .enums import CoverFormat
from .exceptions import GamdlMediaError

if TYPE_CHECKING:
    from .cover_cache import CoverCache


CoverSize = int | Literal["max"]


def validate_cover_size(value: CoverSize, *, name: str = "cover size") -> CoverSize:
    """Accept a positive pixel size or Apple's maximum-resolution request."""
    if value == "max" or (type(value) is int and value > 0):
        return value
    raise ValueError(f"{name} must be a positive integer or 'max'")


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

    def get_formatted_url(
        self,
        *,
        width: CoverSize,
        height: CoverSize,
        format: CoverFormat | str = CoverFormat.JPG,
    ) -> str:
        """Expand Apple's width/height/format placeholders without HTTP IO."""
        if isinstance(format, CoverFormat):
            format = format.value
        if format not in {"jpg", "jpeg", "png", "webp"}:
            raise ValueError("Unsupported artwork format")
        validate_cover_size(width, name="Artwork width")
        validate_cover_size(height, name="Artwork height")
        if width == "max" or height == "max":
            if width != "max" or height != "max":
                raise ValueError(
                    "Maximum artwork size requires both dimensions to be 'max'"
                )
            # Unlike square {w}x{h} artwork, Apple's maximum-resolution
            # variant replaces the entire final size/format component.
            if "{w}x{h}" not in self.url:
                raise ValueError("Maximum artwork size requires an Apple size template")
            return re.sub(
                r"\{w\}x\{h\}[^/?#]*",
                f"10000x0w-999.{format}",
                self.url,
            )
        # Apple Music uses both {w}x{h}.{f} and {w}x{h}bb.jpg (or
        # similar fixed-suffix variants). The latter still supports requesting
        # PNG by changing the resized asset's extension, not by attempting
        # the removed raw/original image URL trick.
        template = re.sub(
            r"(\{w\}x\{h\}[^/?]*?)\.(?:jpg|jpeg|png|webp)(?=[?#]|$)",
            lambda match: f"{match.group(1)}.{format}",
            self.url,
            flags=re.IGNORECASE,
        )
        return (
            template.replace("{w}", str(width))
            .replace("{h}", str(height))
            .replace("{f}", format)
        )

    async def get_cover_bytes(
        self,
        *,
        width: CoverSize = 1200,
        height: CoverSize = 1200,
        format: CoverFormat | str = CoverFormat.JPG,
    ) -> bytes | None:
        """Retrieve artwork bytes with the optional attached CoverCache."""
        url = self.get_formatted_url(width=width, height=height, format=format)
        if self.cache is not None:
            return await self.cache.get_bytes(url)
        return await fetch_cover_bytes(url)
