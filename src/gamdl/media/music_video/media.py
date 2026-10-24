"""Catalog and library music-video metadata model."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING
from ..base import Media, _first_resource

if TYPE_CHECKING:
    import httpx
    from gamdl.api import ItunesApi
    from gamdl.api.wrapper import WrapperApi
    from .stream import MusicVideoStreams


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

    async def get_streams(
        self,
        *,
        video_codec: str = "h264",
        max_height: int = 1080,
        wrapper_api: WrapperApi | None = None,
        itunes_api: ItunesApi | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> MusicVideoStreams:
        """Select separate HLS video and audio tracks from the same master."""
        from .stream import MusicVideoStreamResolver

        return await MusicVideoStreamResolver(
            self, wrapper_api=wrapper_api, itunes_api=itunes_api, client=client
        ).get_streams(video_codec=video_codec, max_height=max_height)
