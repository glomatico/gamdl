import os
import shutil
from pathlib import Path
from typing import AsyncGenerator

import structlog

from ..interface.types import AppleMusicMedia
from .constants import PLAYLIST_HEADER, TEMP_PATH_TEMPLATE
from .enums import DownloadMode
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
        self._playlist_last_tracks: dict[Path, int] = {}

    async def get_download_item_from_url(
        self,
        url: str,
    ) -> AsyncGenerator[DownloadItem, None]:
        # A URL starts a new playlist-writing session. The first playable
        # entry will replace any stale playlist from an earlier run.
        self._playlist_last_tracks.clear()
        async for media in self.base.interface.get_media_from_url(url):
            yield await self.parse_download_item(media)

    async def parse_download_item(
        self,
        media: AppleMusicMedia,
    ) -> DownloadItem:
        if media.error:
            return DownloadItem(media)

        if media.partial:
            return DownloadItem(media)

        elif media.media_metadata["type"] in {"songs", "library-songs"}:
            return await self.song.get_download_item(media)

        elif media.media_metadata["type"] in {
            "music-videos",
            "library-music-videos",
        }:
            return await self.music_video.get_download_item(media)

        elif media.media_metadata["type"] in {"uploaded-videos"}:
            return await self.uploaded_video.get_download_item(media)

    async def download(self, item: DownloadItem) -> None:
        try:
            if item.media.error:
                raise item.media.error

            if item.media.partial:
                return

            await self._initial_processing(item)
            await self._download(item)
            await self._final_processing(item)
        finally:
            if not self.skip_cleanup:
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

        if playlist_track < 1:
            raise ValueError("playlist_track must be one-based")

        playlist_file_path_obj.parent.mkdir(parents=True, exist_ok=True)
        final_path_relative = os.path.relpath(
            final_path_obj.absolute(),
            start=playlist_file_path_obj.parent.absolute(),
        )
        playlist_line = final_path_relative.replace(os.sep, "/") + "\n"
        last_track = self._playlist_last_tracks.get(playlist_file_path_obj)

        if last_track is None:
            # Playlist items normally arrive in order. Start a fresh file and
            # retain blank slots for any items that could not be prepared.
            with playlist_file_path_obj.open(
                "w", encoding="utf8", newline="\n"
            ) as playlist_file:
                playlist_file.write(PLAYLIST_HEADER)
                playlist_file.write("\n" * (playlist_track - 1))
                playlist_file.write(playlist_line)
        elif playlist_track > last_track:
            # Appending makes a full playlist O(n) instead of repeatedly
            # reading and rewriting an ever-growing file (O(n²)).
            with playlist_file_path_obj.open(
                "a", encoding="utf8", newline="\n"
            ) as playlist_file:
                playlist_file.write("\n" * (playlist_track - last_track - 1))
                playlist_file.write(playlist_line)
        else:
            # Keep the helper correct for callers that supply entries out of
            # order, while leaving the common ordered path append-only.
            playlist_file_lines = playlist_file_path_obj.read_text(
                encoding="utf8"
            ).splitlines(keepends=True)
            if not playlist_file_lines or playlist_file_lines[0] != PLAYLIST_HEADER:
                playlist_file_lines.insert(0, PLAYLIST_HEADER)
            if len(playlist_file_lines) <= playlist_track:
                playlist_file_lines.extend(
                    "\n"
                    for _ in range(playlist_track - len(playlist_file_lines) + 1)
                )
            playlist_file_lines[playlist_track] = playlist_line
            playlist_file_path_obj.write_text(
                "".join(playlist_file_lines),
                encoding="utf8",
                newline="\n",
            )

        self._playlist_last_tracks[playlist_file_path_obj] = max(
            last_track or 0,
            playlist_track,
        )

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
            raise GamdlDownloaderSyncedLyricsOnlyError()

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
        if temp_path.exists() and temp_path.is_dir():
            shutil.rmtree(temp_path, ignore_errors=True)
            log.debug("success")
