from pathlib import Path

from ..interface.enums import UploadedVideoQuality
from ..interface.interface_uploaded_video import AppleMusicUploadedVideoInterface
from .downloader_base import AppleMusicBaseDownloader
from .types import DownloadItem


class AppleMusicUploadedVideoDownloader(AppleMusicBaseDownloader):
    def __init__(
        self,
        base_downloader: AppleMusicBaseDownloader,
        interface: AppleMusicUploadedVideoInterface,
        quality: UploadedVideoQuality = UploadedVideoQuality.BEST,
    ):
        self.__dict__.update(base_downloader.__dict__)
        self.interface = interface
        self.quality = quality

    def get_cover_path(self, final_path: str, file_extension: str) -> str:
        return str(Path(final_path).with_suffix(file_extension))

    async def get_download_item(
        self,
        uploaded_video_metadata: dict,
    ) -> DownloadItem:
        try:
            return await self._get_download_item(
                uploaded_video_metadata,
            )
        except Exception as e:
            return DownloadItem(
                media_metadata=uploaded_video_metadata,
                error=e,
            )

    async def _get_download_item(
        self,
        uploaded_video_metadata: dict,
    ) -> DownloadItem:
        download_item = DownloadItem()

        download_item.media_metadata = uploaded_video_metadata

        download_item.media_tags = self.interface.get_tags(
            uploaded_video_metadata,
        )

        download_item.stream_info = await self.interface.get_stream_info(
            uploaded_video_metadata,
            self.quality,
        )

        download_item.random_uuid = self.get_random_uuid()
        download_item.staged_path = self.get_temp_path(
            uploaded_video_metadata["id"],
            download_item.random_uuid,
            "staged",
            "." + download_item.stream_info.file_format.value,
        )
        download_item.final_path = self.get_final_path(
            download_item.media_tags,
            Path(download_item.staged_path).suffix,
            None,
        )

        download_item.cover_url_template = self.interface.get_cover_url_template(
            uploaded_video_metadata,
            self.cover_format,
        )
        download_item.cover_url = self.interface.get_cover_url(
            download_item.cover_url_template,
            self.cover_size,
            self.cover_format,
        )

        cover_file_extension = await self.interface.get_cover_file_extension(
            download_item.cover_url,
            self.cover_format,
        )
        if cover_file_extension:
            download_item.cover_path = self.get_cover_path(
                download_item.final_path,
                cover_file_extension,
            )

        return download_item

    async def download(
        self,
        download_item: DownloadItem,
    ) -> None:
        await self.download_ytdlp(
            download_item.stream_info.video_track.stream_url,
            download_item.staged_path,
        )

        cover_bytes = await self.interface.get_cover_bytes(download_item.cover_url)
        await self.apply_tags(
            download_item.staged_path,
            download_item.media_tags,
            cover_bytes,
        )
