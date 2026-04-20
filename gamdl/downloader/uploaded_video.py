from pathlib import Path

from ..interface.enums import CoverFormat
from ..interface.types import AppleMusicMedia
from .base import AppleMusicBaseDownloader
from .types import DownloadItem


class AppleMusicUploadedVideoDownloader:
    def __init__(
        self,
        base: AppleMusicBaseDownloader,
    ):
        self.base = base

    def get_cover_path(self, final_path: str, file_extension: str) -> str:
        return str(Path(final_path).with_suffix(file_extension))

    async def get_download_item(
        self,
        media: AppleMusicMedia,
    ) -> DownloadItem:
        download_item = DownloadItem(media)

        download_item.staged_path = self.base.get_temp_path(
            media.media_metadata["id"],
            download_item.uuid_,
            "staged",
            "." + media.stream_info.file_format.value,
        )

        download_item.final_path = self.base.get_final_path(
            media.tags,
            "." + media.stream_info.file_format.value,
            media.playlist_tags,
        )

        download_item.cover_path = self.get_cover_path(
            download_item.final_path,
            media.cover.file_extension,
        )

        return download_item

    async def download(
        self,
        download_item: DownloadItem,
    ) -> None:
        await self.base._download_ytdlp_async(
            download_item.media.stream_info.video_track.stream_url,
            download_item.staged_path,
        )

        cover_bytes = (
            await self.base.interface.base.get_cover_bytes(
                download_item.media.cover.url
            )
            if self.base.interface.base.cover_format != CoverFormat.RAW
            else None
        )
        await self.base.apply_tags(
            download_item.staged_path,
            download_item.media.tags,
            cover_bytes,
        )
