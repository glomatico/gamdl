import shutil
from pathlib import Path
from typing import AsyncGenerator

import structlog

from .constants import TEMP_PATH_TEMPLATE
from .enums import DownloadMode, RemuxMode
from .exceptions import (
    GamdlDownloaderDependencyNotFoundError,
    GamdlDownloaderMediaFileExistsError,
    GamdlDownloaderSyncedLyricsOnlyError,
)
from .music_video import AppleMusicMusicVideoDownloader
from .song import AppleMusicSongDownloader
from .types import DownloadItem
from .uploaded_video import AppleMusicUploadedVideoDownloader

logger = structlog.get_logger(__name__)


class AppleMusicDownloader:
    def __init__(
        self,
        song: AppleMusicSongDownloader,
        music_video: AppleMusicMusicVideoDownloader,
        uploaded_video: AppleMusicUploadedVideoDownloader,
        overwrite: bool = False,
        save_cover: bool = False,
        save_playlist: bool = False,
        no_synced_lyrics: bool = False,
        synced_lyrics_only: bool = False,
        skip_cleanup: bool = False,
        skip_processing: bool = False,
    ):
        self.song = song
        self.music_video = music_video
        self.uploaded_video = uploaded_video
        self.overwrite = overwrite
        self.save_cover = save_cover
        self.save_playlist = save_playlist
        self.no_synced_lyrics = no_synced_lyrics
        self.synced_lyrics_only = synced_lyrics_only
        self.skip_cleanup = skip_cleanup
        self.skip_processing = skip_processing

        self.base = song.base

    async def get_download_item_from_url(
        self,
        url: str,
    ) -> AsyncGenerator[DownloadItem, None]:
        async for media in self.base.interface.get_media_from_url(url):
            if media.error or media.flat_filter_result:
                yield DownloadItem(media)

            elif media.media_metadata["type"] in {"songs", "library-songs"}:
                yield await self.song.get_download_item(media)

            elif media.media_metadata["type"] in {
                "music-videos",
                "library-music-videos",
            }:
                yield await self.music_video.get_download_item(media)

            elif media.media_metadata["type"] in {"uploaded-videos"}:
                yield await self.uploaded_video.get_download_item(media)

    async def download(self, item: DownloadItem) -> None:
        try:
            if item.media.error:
                raise item.media.error

            await self._initial_processing(item)
            await self._download(item)
            await self._final_processing(item)
        finally:
            self._cleanup_temp(item.uuid_)

    def _update_playlist_file(
        self,
        playlist_file_path: str,
        final_path: str,
        playlist_track: int,
    ) -> None:
        log = logger.bind(
            action="update_playlist_file",
            playlist_file_path=playlist_file_path,
            final_path=final_path,
            playlist_track=playlist_track,
        )

        playlist_file_path_obj = Path(playlist_file_path)
        final_path_obj = Path(final_path)
        output_dir_obj = Path(self.base.output_path)

        playlist_file_path_obj.parent.mkdir(parents=True, exist_ok=True)
        playlist_file_path_parent_parts_len = len(playlist_file_path_obj.parent.parts)
        output_path_parts_len = len(output_dir_obj.parts)

        final_path_relative = Path(
            ("../" * (playlist_file_path_parent_parts_len - output_path_parts_len)),
            *final_path_obj.parts[output_path_parts_len:],
        )
        playlist_file_lines = (
            playlist_file_path_obj.open("r", encoding="utf8").readlines()
            if playlist_file_path_obj.exists()
            else []
        )
        if len(playlist_file_lines) < playlist_track:
            playlist_file_lines.extend(
                "\n" for _ in range(playlist_track - len(playlist_file_lines))
            )

        playlist_file_lines[playlist_track - 1] = final_path_relative.as_posix() + "\n"
        with playlist_file_path_obj.open("w", encoding="utf8") as playlist_file:
            playlist_file.writelines(playlist_file_lines)

        log.debug("success")

    def _write_cover(self, cover_path: str, cover_bytes: bytes) -> None:
        log = logger.bind(action="write_cover_file", cover_path=cover_path)

        Path(cover_path).parent.mkdir(parents=True, exist_ok=True)
        with open(cover_path, "wb") as f:
            f.write(cover_bytes)

        log.debug("success")

    def _write_synced_lyrics(self, synced_lyrics_path: str, lyrics: str) -> None:
        log = logger.bind(
            action="write_synced_lyrics",
            synced_lyrics_path=synced_lyrics_path,
        )

        Path(synced_lyrics_path).parent.mkdir(parents=True, exist_ok=True)
        with open(synced_lyrics_path, "w", encoding="utf-8") as f:
            f.write(lyrics)

        log.debug("success")

    async def _initial_processing(self, item: DownloadItem) -> None:
        if self.skip_processing:
            return

        if item.playlist_file_path and item.final_path and self.save_playlist:
            self._update_playlist_file(
                item.playlist_file_path,
                item.final_path,
                item.media.playlist_tags.track,
            )

        if item.cover_path and self.save_cover and item.media.cover.url:
            cover_bytes = await self.base.interface.base.get_cover_bytes(
                item.media.cover.url,
            )
            if cover_bytes and (self.overwrite or not Path(item.cover_path).exists()):
                self._write_cover(
                    item.cover_path,
                    cover_bytes,
                )

        if (
            item.synced_lyrics_path
            and not self.no_synced_lyrics
            and item.media.lyrics
            and item.media.lyrics.synced
            and (self.overwrite or not Path(item.synced_lyrics_path).exists())
        ):
            self._write_synced_lyrics(
                item.synced_lyrics_path,
                item.media.lyrics.synced,
            )

    async def _download(self, item: DownloadItem) -> None:
        if item.media.error:
            raise item.media.error

        if self.synced_lyrics_only:
            raise GamdlDownloaderSyncedLyricsOnlyError(
                "Download mode is set to synced lyrics only"
            )

        if Path(item.final_path).exists() and not self.overwrite:
            raise GamdlDownloaderMediaFileExistsError(item.final_path)

        if item.media.media_metadata["type"] in {
            "music-videos",
            "library-music-videos",
            "songs",
            "library-songs",
        }:
            if (
                self.base.download_mode == DownloadMode.NM3U8DLRE
                and not self.base.full_nm3u8dlre_path
            ):
                raise GamdlDownloaderDependencyNotFoundError("N_m3u8DL-RE")

            if item.media.media_metadata["type"] in {"songs", "library-songs"}:
                await self.song.download(item)

            elif item.media.media_metadata["type"] in {
                "music-videos",
                "library-music-videos",
            }:
                if (
                    self.music_video.remux_mode == RemuxMode.FFMPEG
                    and not self.base.full_ffmpeg_path
                ):
                    raise GamdlDownloaderDependencyNotFoundError("FFmpeg")

                if (
                    self.music_video.remux_mode == RemuxMode.MP4BOX
                    and not self.base.full_mp4box_path
                    and not self.base.full_mp4decrypt_path
                ):
                    raise GamdlDownloaderDependencyNotFoundError(
                        "MP4Box and/or mp4decrypt"
                    )

                await self.music_video.download(item)

        elif item.media.media_metadata["type"] in {"uploaded-videos"}:
            await self.uploaded_video.download(item)

    def _move_to_final_path(self, staged_path: str, final_path: str) -> None:
        log = logger.bind(
            action="move_to_final_path",
            staged_path=staged_path,
            final_path=final_path,
        )

        Path(final_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(staged_path, final_path)

        log.debug("success")

    async def _final_processing(
        self,
        item: DownloadItem,
    ) -> None:
        if self.skip_processing:
            return

        if Path(item.staged_path).exists():
            self._move_to_final_path(
                item.staged_path,
                item.final_path,
            )

    def _cleanup_temp(self, folder_tag: str) -> None:
        log = logger.bind(action="cleanup_temp", folder_tag=folder_tag)

        temp_path = Path(self.base.temp_path) / TEMP_PATH_TEMPLATE.format(folder_tag)
        if temp_path.exists() and temp_path.is_dir() and not self.skip_cleanup:
            shutil.rmtree(temp_path, ignore_errors=True)

        log.debug("success")
