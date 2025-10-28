import logging

from InquirerPy import inquirer
from InquirerPy.base.control import Choice

from ..interface.enums import UploadedVideoQuality
from ..interface.types import MediaTags
from .constants import UPLOADED_VIDEO_QUALITY_RANK
from .interface import AppleMusicInterface
from .types import MediaFileFormat, StreamInfo, StreamInfoAv

logger = logging.getLogger(__name__)


class AppleMusicUploadedVideoInterface(AppleMusicInterface):
    def __init__(self, interface: AppleMusicInterface):
        self.__dict__.update(interface.__dict__)

    def get_stream_url_best(self, metadata: dict) -> str:
        best_quality = next(
            (
                quality
                for quality in UPLOADED_VIDEO_QUALITY_RANK
                if metadata["attributes"]["assetTokens"].get(quality)
            ),
            None,
        )
        return metadata["attributes"]["assetTokens"][best_quality]

    async def get_stream_url_from_user(self, metadata: dict) -> str:
        qualities = list(metadata["attributes"]["assetTokens"].keys())
        choices = [
            Choice(
                name=quality,
                value=quality,
            )
            for quality in qualities
        ]
        selected = await inquirer.select(
            message="Select which quality to download:",
            choices=choices,
        ).execute_async()

        return metadata["attributes"]["assetTokens"][selected]

    async def get_stream_url(
        self, metadata: dict, quality: UploadedVideoQuality
    ) -> str:
        if quality == UploadedVideoQuality.BEST:
            stream_url = self.get_stream_url_best(metadata)

        if quality == UploadedVideoQuality.ASK:
            stream_url = await self.get_stream_url_from_user(metadata)

        logger.debug(f"Stream URL: {stream_url}")

        return stream_url

    async def get_stream_info(
        self,
        metadata: dict,
        quality: UploadedVideoQuality,
    ) -> StreamInfo:
        stream_url = await self.get_stream_url(metadata, quality)
        stream_info = StreamInfoAv(
            file_format=MediaFileFormat.M4V,
            video_track=StreamInfo(
                stream_url=stream_url,
            ),
        )
        return stream_info

    def get_tags(self, metadata: dict) -> MediaTags:
        attributes = metadata["attributes"]
        upload_date = attributes.get("uploadDate")

        tags = MediaTags(
            artist=attributes.get("artistName"),
            date=self.parse_date(upload_date) if upload_date else None,
            title=attributes.get("name"),
            title_id=int(metadata["id"]),
            storefront=int(self.itunes_api.storefront_id.split("-")[0]),
        )
        logger.debug(f"Tags: {tags}")

        return tags
