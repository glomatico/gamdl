"""Catalog and library music-video metadata model."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TYPE_CHECKING
from ..base import Media, _first_resource
from .enums import MusicVideoCodec
from .models import MusicVideoStreamSelector

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
        video_codec: MusicVideoCodec | None = None,
        codec_priority: Sequence[MusicVideoCodec] | None = None,
        max_height: int | None = 1080,
        ask: bool = False,
        selector: MusicVideoStreamSelector | None = None,
        wrapper_api: WrapperApi | None = None,
        itunes_api: ItunesApi | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> MusicVideoStreams:
        """Select video and associated audio, or ask separately for both.

        ``video_codec`` selects one exact codec, while ``codec_priority``
        tries codecs in order (default: H264, H265). In interactive mode,
        neither preference is used; ``max_height`` still limits offered video.
        """
        from .stream import MusicVideoStreamResolver

        return await MusicVideoStreamResolver(
            self, wrapper_api=wrapper_api, itunes_api=itunes_api, client=client
        ).get_streams(
            video_codec=video_codec,
            codec_priority=codec_priority,
            max_height=max_height,
            ask=ask,
            selector=selector,
        )
