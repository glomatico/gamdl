import asyncio

from ..utils import safe_gather
from .downloader_base import AppleMusicBaseDownloader
from .downloader_song import AppleMusicSongDownloader
from .downloader_music_video import AppleMusicMusicVideoDownloader
from .types import DownloadItem
from pathlib import Path
from .exceptions import (
    MediaNotStreamableError,
    MediaFormatNotAvailableError,
    MediaFileAlreadyExistsError,
)


class AppleMusicDownloader:
    def __init__(
        self,
        base_downloader: AppleMusicBaseDownloader,
        song_downloader: AppleMusicSongDownloader,
        music_video_downloader: AppleMusicMusicVideoDownloader,
    ):
        self.base_downloader = base_downloader
        self.song_downloader = song_downloader
        self.music_video_downloader = music_video_downloader

    async def get_single_download_item(
        self,
        media_metadata: dict,
    ) -> DownloadItem:
        download_item = None

        if media_metadata["type"] in {"songs", "library-songs"}:
            download_item = await self.song_downloader.get_download_item(
                media_metadata,
            )

        if media_metadata["type"] in {"music-videos", "library-music-videos"}:
            download_item = await self.music_video_downloader.get_download_item(
                media_metadata,
            )

        return download_item

    async def get_album_download_items(
        self,
        album_metadata: dict,
    ) -> list[DownloadItem | Exception]:
        tasks = []
        for media_metadata in album_metadata["relationships"]["tracks"]["data"]:
            tasks.append(
                asyncio.create_task(
                    self.song_downloader.get_download_item(
                        media_metadata,
                    )
                )
            )

        download_items = await safe_gather(*tasks)
        return download_items

    async def download(self, download_item: DownloadItem) -> None:
        try:
            if isinstance(download_item, Exception):
                raise download_item

            await self._download(download_item)
            if not self.base_downloader.skip_processing:
                await self._final_processing(download_item)
        finally:
            if isinstance(download_item, DownloadItem):
                self.base_downloader.cleanup_temp(download_item.random_uuid)

    async def _download(
        self,
        download_item: DownloadItem,
    ) -> None:
        if (
            Path(download_item.final_path).exists()
            and not self.base_downloader.overwrite
        ):
            raise MediaFileAlreadyExistsError(download_item.final_path)
        if not self.base_downloader.is_media_streamable(
            download_item.media_metadata,
        ):
            raise MediaNotStreamableError(
                download_item.media_metadata["id"],
            )
        if (
            not download_item.stream_info
            or not download_item.stream_info.audio_track.widevine_pssh
        ):
            raise MediaFormatNotAvailableError(
                download_item.media_metadata["id"],
            )

        if download_item.media_metadata["type"] in {"songs", "library-songs"}:
            await self.song_downloader.download(download_item)

        if download_item.media_metadata["type"] in {
            "music-videos",
            "library-music-videos",
        }:
            await self.music_video_downloader.download(download_item)

    async def _final_processing(
        self,
        download_item: DownloadItem,
    ) -> None:
        if Path(download_item.staged_path).exists():
            self.base_downloader.move_to_final_path(
                download_item.staged_path,
                download_item.final_path,
            )

        if download_item.cover_path and self.base_downloader.save_cover:
            cover_url = self.base_downloader.get_cover_url(
                download_item.cover_url_template,
            )
            cover_bytes = await self.base_downloader.get_cover_bytes(cover_url)
            if cover_bytes:
                self.base_downloader.write_cover_image(
                    cover_bytes,
                    download_item.cover_path,
                )

        if (
            download_item.lyrics
            and download_item.lyrics.synced
            and not self.song_downloader.skip_synced_lyrics
        ):
            self.song_downloader.write_synced_lyrics(
                download_item.lyrics.synced,
                download_item.synced_lyrics_path,
            )
