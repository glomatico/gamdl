import asyncio
from pathlib import Path

from ..utils import safe_gather
from .constants import (
    ALBUM_MEDIA_TYPE,
    MUSIC_VIDEO_MEDIA_TYPE,
    PLAYLIST_MEDIA_TYPE,
    SONG_MEDIA_TYPE,
    UPLOADED_VIDEO_MEDIA_TYPE,
    VALID_URL_PATTERN,
)
from .downloader_base import AppleMusicBaseDownloader
from .downloader_music_video import AppleMusicMusicVideoDownloader
from .downloader_song import AppleMusicSongDownloader
from .downloader_uploaded_video import AppleMusicUploadedVideoDownloader
from .exceptions import MediaFormatNotAvailableError, MediaNotStreamableError
from .types import DownloadItem, UrlInfo


class AppleMusicDownloader:
    def __init__(
        self,
        base_downloader: AppleMusicBaseDownloader,
        song_downloader: AppleMusicSongDownloader,
        music_video_downloader: AppleMusicMusicVideoDownloader,
        uploaded_video_downloader: AppleMusicUploadedVideoDownloader,
    ):
        self.base_downloader = base_downloader
        self.song_downloader = song_downloader
        self.music_video_downloader = music_video_downloader
        self.uploaded_video_downloader = uploaded_video_downloader

    async def get_single_download_item(
        self,
        media_metadata: dict,
        playlist_metadata: dict = None,
    ) -> DownloadItem:
        download_item = None

        if media_metadata["type"] in SONG_MEDIA_TYPE:
            download_item = await self.song_downloader.get_download_item(
                media_metadata,
            )

        if media_metadata["type"] in MUSIC_VIDEO_MEDIA_TYPE:
            download_item = await self.music_video_downloader.get_download_item(
                media_metadata,
            )

        if media_metadata["type"] in UPLOADED_VIDEO_MEDIA_TYPE:
            download_item = await self.uploaded_video_downloader.get_download_item(
                media_metadata,
            )

        return download_item

    async def get_collection_download_items(
        self,
        collection_metadata: dict,
    ) -> list[DownloadItem | Exception]:
        collection_metadata["relationships"]["tracks"]["data"].extend(
            [
                extended_data
                async for extended_data in self.base_downloader.apple_music_api.extend_api_data(
                    collection_metadata["relationships"]["tracks"],
                )
            ]
        )

        tasks = [
            asyncio.create_task(
                self.song_downloader.get_download_item(
                    media_metadata,
                    (
                        collection_metadata
                        if collection_metadata["type"] in PLAYLIST_MEDIA_TYPE
                        else None
                    ),
                )
            )
            for media_metadata in collection_metadata["relationships"]["tracks"]["data"]
        ]

        download_items = await safe_gather(*tasks)
        return download_items

    def get_url_info(self, url: str) -> UrlInfo | None:
        match = VALID_URL_PATTERN.match(url)
        if not match:
            return None

        return UrlInfo(
            **match.groupdict(),
        )

    async def get_download_queue(
        self,
        url_info: UrlInfo,
    ) -> list[DownloadItem | Exception] | None:
        return await self._get_download_queue(
            "song" if url_info.sub_id else url_info.type,
            url_info.sub_id or url_info.id or url_info.library_id,
            url_info.library_id is not None,
        )

    async def _get_download_queue(
        self,
        url_type: str,
        id: str,
        is_library: bool,
    ) -> list[DownloadItem | Exception] | None:
        download_items = []

        if url_type == "artist":
            pass

        if url_type in SONG_MEDIA_TYPE:
            song_respose = await self.base_downloader.apple_music_api.get_song(id)

            if song_respose is None:
                return None

            download_items.append(
                await self.get_single_download_item(song_respose["data"][0])
            )

        if url_type in ALBUM_MEDIA_TYPE:
            if is_library:
                album_response = (
                    await self.base_downloader.apple_music_api.get_library_album(id)
                )
            else:
                album_response = await self.base_downloader.apple_music_api.get_album(
                    id
                )

            if album_response is None:
                return None

            download_items = await self.get_collection_download_items(
                album_response["data"][0],
            )

        if url_type in PLAYLIST_MEDIA_TYPE:
            if is_library:
                playlist_response = (
                    await self.base_downloader.apple_music_api.get_library_playlist(id)
                )
            else:
                playlist_response = (
                    await self.base_downloader.apple_music_api.get_playlist(id)
                )

            if playlist_response is None:
                return None

            download_items = await self.get_collection_download_items(
                playlist_response["data"][0],
            )

        if url_type in MUSIC_VIDEO_MEDIA_TYPE:
            music_video_response = (
                await self.base_downloader.apple_music_api.get_music_video(id)
            )

            if music_video_response is None:
                return None

            download_items.append(
                await self.get_single_download_item(music_video_response["data"][0])
            )

        if url_type in UPLOADED_VIDEO_MEDIA_TYPE:
            uploaded_video = (
                await self.base_downloader.apple_music_api.get_uploaded_video(id)
            )

            if uploaded_video is None:
                return None

            download_items.append(
                await self.get_single_download_item(uploaded_video["data"][0])
            )

        return download_items

    async def download(self, download_item: DownloadItem | Exception) -> None:
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
            raise FileExistsError(
                f'Media file already exists at "{download_item.final_path}"'
            )
        if not self.base_downloader.is_media_streamable(
            download_item.media_metadata,
        ):
            raise MediaNotStreamableError(
                download_item.media_metadata["id"],
            )
        if download_item.media_metadata["type"] in {
            *SONG_MEDIA_TYPE,
            *MUSIC_VIDEO_MEDIA_TYPE,
        } and (
            not download_item.stream_info
            or not download_item.stream_info.audio_track.widevine_pssh
        ):
            raise MediaFormatNotAvailableError(
                download_item.media_metadata["id"],
            )

        if download_item.media_metadata["type"] in SONG_MEDIA_TYPE:
            await self.song_downloader.download(download_item)

        if download_item.media_metadata["type"] in MUSIC_VIDEO_MEDIA_TYPE:
            await self.music_video_downloader.download(download_item)

        if download_item.media_metadata["type"] in UPLOADED_VIDEO_MEDIA_TYPE:
            await self.uploaded_video_downloader.download(download_item)

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
            and not self.song_downloader.no_synced_lyrics
        ):
            self.song_downloader.write_synced_lyrics(
                download_item.lyrics.synced,
                download_item.synced_lyrics_path,
            )

        if download_item.playlist_tags and self.base_downloader.save_playlist:
            self.base_downloader.update_playlist_file(
                download_item.playlist_file_path,
                download_item.final_path,
                download_item.playlist_tags.playlist_track,
            )
