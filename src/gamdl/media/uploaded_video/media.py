"""Uploaded catalog-video metadata model."""

from __future__ import annotations

from typing import Any
from ..base import Media, _first_resource
from ..exceptions import UnsupportedMediaError
from collections.abc import Sequence
from .stream import (
    UploadedVideoQuality,
    UploadedVideoStreamResolver,
    UploadedVideoStreamSelector,
)
from ..stream import HttpStream


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

    async def get_stream(
        self,
        *,
        quality: UploadedVideoQuality | None = None,
        quality_priority: Sequence[UploadedVideoQuality] | None = None,
        ask: bool = False,
        selector: UploadedVideoStreamSelector | None = None,
    ) -> HttpStream:
        """Select a DRM-free progressive asset using GAMDL 3.x quality ranking."""
        return await UploadedVideoStreamResolver(self).get_stream(
            quality=quality,
            quality_priority=quality_priority,
            ask=ask,
            selector=selector,
        )
