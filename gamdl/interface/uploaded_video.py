import asyncio
from collections.abc import Callable
from typing import AsyncGenerator

import structlog

from .base import AppleMusicBaseInterface
from .constants import UPLOADED_VIDEO_QUALITY_RANK
from .enums import UploadedVideoQuality
from .exceptions import (
    GamdlInterfaceFormatNotAvailableError,
    GamdlInterfaceMediaNotStreamableError,
)
from .types import AppleMusicMedia, MediaFileFormat, MediaTags, StreamInfo, StreamInfoAv

logger = structlog.get_logger(__name__)


class AppleMusicUploadedVideoInterface:
    def __init__(
        self,
        base: AppleMusicBaseInterface,
        quality: UploadedVideoQuality = UploadedVideoQuality.BEST,
        ask_quality_function: Callable[[dict], dict | None] | None = None,
    ):
        self.base = base
        self.quality = quality
        self.ask_quality_function = ask_quality_function

    def _get_best_stream_url(self, metadata: dict) -> str:
        best_quality = next(
            (
                quality
                for quality in UPLOADED_VIDEO_QUALITY_RANK
                if metadata["attributes"]["assetTokens"].get(quality)
            ),
            None,
        )
        return metadata["attributes"]["assetTokens"][best_quality]

    async def _get_stream_url_from_user(self, metadata: dict) -> str | None:
        if self.ask_quality_function:
            selected_quality = self.ask_quality_function(
                metadata["attributes"]["assetTokens"]
            )
            if asyncio.iscoroutine(selected_quality):
                selected_quality = await selected_quality
            return selected_quality

        return None

    async def _get_stream_url(
        self,
        metadata: dict,
    ) -> str | None:
        if self.quality == UploadedVideoQuality.BEST:
            stream_url = self._get_best_stream_url(metadata)

        if self.quality == UploadedVideoQuality.ASK:
            stream_url = await self._get_stream_url_from_user(metadata)

        return stream_url

    async def get_stream_info(
        self,
        metadata: dict,
    ) -> StreamInfo | None:
        log = logger.bind(
            action="get_uploaded_video_stream_info", media_id=metadata["id"]
        )

        stream_url = await self._get_stream_url(metadata)
        if not stream_url:
            log.debug("no_stream_url_available")

            return None

        stream_info = StreamInfoAv(
            file_format=MediaFileFormat.M4V,
            video_track=StreamInfo(
                stream_url=stream_url,
            ),
        )

        log.debug("success", stream_info=stream_info)

        return stream_info

    def get_tags(self, metadata: dict) -> MediaTags:
        log = logger.bind(action="get_uploaded_video_tags", media_id=metadata["id"])

        attributes = metadata["attributes"]
        upload_date = attributes.get("uploadDate")

        tags = MediaTags(
            artist=attributes.get("artistName"),
            date=self.base.parse_date(upload_date) if upload_date else None,
            title=attributes.get("name"),
            title_id=int(metadata["id"]),
            storefront=self.base.itunes_api.storefront_id,
        )

        log.debug("success", tags=tags)

        return tags

    async def get_media(
        self,
        media: AppleMusicMedia,
    ) -> AsyncGenerator[AppleMusicMedia, None]:
        if not media.media_metadata:
            media.media_metadata = (
                await self.base.apple_music_api.get_uploaded_video(media.media_id)
            )["data"][0]

        media.media_id = self.base.parse_catalog_media_id(media.media_metadata)

        yield media

        if not self.base.is_media_streamable(media.media_metadata):
            raise GamdlInterfaceMediaNotStreamableError(media.media_id)

        media.cover = await self.base.get_cover(media.media_metadata)

        media.stream_info = await self.get_stream_info(media.media_metadata)
        if not media.stream_info:
            raise GamdlInterfaceFormatNotAvailableError(media.media_id)

        media.tags = self.get_tags(media.media_metadata)

        media.partial = False

        yield media
