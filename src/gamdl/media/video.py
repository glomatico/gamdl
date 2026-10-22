"""Individual video media types."""

from __future__ import annotations

from typing import Any

from .base import Media
from .exceptions import UnsupportedMediaError


def _first_resource(payload: dict[str, Any]) -> dict[str, Any]:
    from .exceptions import GamdlMediaError

    values = payload.get("data")
    if not isinstance(values, list) or not values:
        raise GamdlMediaError("Apple Music returned no media metadata")
    return values[0]


class MusicVideo(Media):
    """Catalog or library music video with lazy metadata."""

    resource_type = "music-videos"
    library_type = "library-music-videos"

    async def _fetch_metadata(self) -> dict[str, Any]:
        result = (
            await self.api.get_library_music_video(self.id)
            if self.is_library
            else await self.api.get_music_video(self.id)
        )
        return _first_resource(result)


class UploadedVideo(Media):
    """Uploaded catalog video, with a dedicated metadata endpoint."""

    resource_type = "uploaded-videos"
    library_type = ""

    def __init__(self, *args: Any, is_library: bool = False, **kwargs: Any) -> None:
        if is_library:
            raise UnsupportedMediaError("Uploaded videos do not support library IDs")
        super().__init__(*args, is_library=False, **kwargs)

    async def _fetch_metadata(self) -> dict[str, Any]:
        return _first_resource(await self.api.get_uploaded_video(self.id))
