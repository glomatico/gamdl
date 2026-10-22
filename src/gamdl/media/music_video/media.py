"""Catalog and library music-video metadata model."""

from __future__ import annotations

from typing import Any
from ..base import Media, _first_resource


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
