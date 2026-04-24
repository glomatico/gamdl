import asyncio
import re
import shutil
from pathlib import Path

import structlog
from mutagen.mp4 import MP4, MP4Cover
from yt_dlp import YoutubeDL

from ..interface.enums import CoverFormat
from ..interface.interface import AppleMusicInterface
from ..interface.types import MediaTags, PlaylistTags
from ..utils import CustomStringFormatter, async_subprocess
from .constants import ILLEGAL_CHAR_REPLACEMENT, ILLEGAL_CHARS_RE, TEMP_PATH_TEMPLATE
from .enums import DownloadMode

logger = structlog.get_logger(__name__)


class AppleMusicBaseDownloader:
    def __init__(
        self,
        interface: AppleMusicInterface,
        output_path: str = "./Apple Music",
        temp_path: str = ".",
        nm3u8dlre_path: str = "N_m3u8DL-RE",
        mp4decrypt_path: str = "mp4decrypt",
        ffmpeg_path: str = "ffmpeg",
        mp4box_path: str = "MP4Box",
        wrapper_decrypt_ip: str = "127.0.0.1:10020",
        download_mode: DownloadMode = DownloadMode.YTDLP,
        album_folder_template: str = "{album_artist}/{album}",
        compilation_folder_template: str = "Compilations/{album}",
        no_album_folder_template: str = "{artist}/Unknown Album",
        playlist_folder_template: str = "Playlists/{playlist_artist}",
        single_disc_file_template: str = "{track:02d} {title}",
        multi_disc_file_template: str = "{disc}-{track:02d} {title}",
        no_album_file_template: str = "{title}",
        playlist_file_template: str = "{playlist_title}",
        date_tag_template: str = "%Y-%m-%dT%H:%M:%SZ",
        exclude_tags: list[str] = None,
        truncate: int = None,
        silent: bool = False,
    ):
        self.interface = interface
        self.output_path = output_path
        self.temp_path = temp_path
        self.nm3u8dlre_path = nm3u8dlre_path
        self.mp4decrypt_path = mp4decrypt_path
        self.ffmpeg_path = ffmpeg_path
        self.mp4box_path = mp4box_path
        self.wrapper_decrypt_ip = wrapper_decrypt_ip
        self.download_mode = download_mode
        self.album_folder_template = album_folder_template
        self.compilation_folder_template = compilation_folder_template
        self.no_album_folder_template = no_album_folder_template
        self.single_disc_file_template = single_disc_file_template
        self.multi_disc_file_template = multi_disc_file_template
        self.playlist_folder_template = playlist_folder_template
        self.no_album_file_template = no_album_file_template
        self.playlist_file_template = playlist_file_template
        self.date_tag_template = date_tag_template
        self.exclude_tags = exclude_tags
        self.truncate = truncate
        self.silent = silent

        self._initialize_binary_paths()

    def _initialize_binary_paths(self):
        log = logger.bind(action="initialize_binary_paths")

        self.full_nm3u8dlre_path = shutil.which(self.nm3u8dlre_path)
        self.full_mp4decrypt_path = shutil.which(self.mp4decrypt_path)
        self.full_ffmpeg_path = shutil.which(self.ffmpeg_path)
        self.full_mp4box_path = shutil.which(self.mp4box_path)

        log = log.debug(
            "success",
            full_nm3u8dlre_path=self.full_nm3u8dlre_path,
            full_mp4decrypt_path=self.full_mp4decrypt_path,
            full_ffmpeg_path=self.full_ffmpeg_path,
            full_mp4box_path=self.full_mp4box_path,
        )

    def get_temp_path(
        self,
        media_id: str,
        folder_tag: str,
        file_tag: str,
        file_extension: str,
    ) -> str:
        log = logger.bind(action="get_temp_path")

        temp_path = str(
            Path(self.temp_path)
            / TEMP_PATH_TEMPLATE.format(folder_tag)
            / (f"{media_id}_{file_tag}" + file_extension)
        )

        log.debug("success", temp_path=temp_path)

        return temp_path

    def _sanitize_string(
        self,
        dirty_string: str,
        file_ext: str = None,
    ) -> str:
        sanitized_string = re.sub(
            ILLEGAL_CHARS_RE,
            ILLEGAL_CHAR_REPLACEMENT,
            dirty_string,
        )

        if file_ext is None:
            sanitized_string = sanitized_string[: self.truncate]
            if sanitized_string.endswith("."):
                sanitized_string = sanitized_string[:-1] + ILLEGAL_CHAR_REPLACEMENT
        else:
            if self.truncate is not None:
                sanitized_string = sanitized_string[: self.truncate - len(file_ext)]
            sanitized_string += file_ext

        return sanitized_string.strip()

    def get_final_path(
        self,
        tags: MediaTags,
        file_extension: str,
        playlist_tags: PlaylistTags | None,
    ) -> str:
        log = logger.bind(action="get_final_path")

        if tags.album:
            template_folder_parts = (
                self.compilation_folder_template.split("/")
                if tags.compilation
                else self.album_folder_template.split("/")
            )
        else:
            template_folder_parts = self.no_album_folder_template.split("/")

        if tags.album:
            template_file_parts = (
                self.multi_disc_file_template.split("/")
                if isinstance(tags.disc_total, int) and tags.disc_total > 1
                else self.single_disc_file_template.split("/")
            )
        else:
            template_file_parts = self.no_album_file_template.split("/")

        template_parts = template_folder_parts + template_file_parts
        formatted_parts = []

        for i, part in enumerate(template_parts):
            is_folder = i < len(template_parts) - 1
            formatted_part = CustomStringFormatter().format(
                part,
                album=(tags.album, "Unknown Album"),
                album_artist=(tags.album_artist, "Unknown Artist"),
                album_id=(tags.album_id, "Unknown Album ID"),
                artist=(tags.artist, "Unknown Artist"),
                artist_id=(tags.artist_id, "Unknown Artist ID"),
                composer=(tags.composer, "Unknown Composer"),
                composer_id=(tags.composer_id, "Unknown Composer ID"),
                date=(tags.date, "Unknown Date"),
                disc=(tags.disc, ""),
                disc_total=(tags.disc_total, ""),
                media_type=(tags.media_type, "Unknown Media Type"),
                playlist_artist=(
                    (playlist_tags.artist if playlist_tags else None),
                    "Unknown Playlist Artist",
                ),
                playlist_id=(
                    (playlist_tags.playlist_id if playlist_tags else None),
                    "Unknown Playlist ID",
                ),
                playlist_title=(
                    (playlist_tags.title if playlist_tags else None),
                    "Unknown Playlist Title",
                ),
                playlist_track=(
                    (playlist_tags.track if playlist_tags else None),
                    "",
                ),
                title=(tags.title, "Unknown Title"),
                title_id=(tags.title_id, "Unknown Title ID"),
                track=(tags.track, ""),
                track_total=(tags.track_total, ""),
            )
            sanitized_formatted_part = self._sanitize_string(
                formatted_part,
                file_extension if not is_folder else None,
            )
            formatted_parts.append(sanitized_formatted_part)

        final_path = str(Path(self.output_path, *formatted_parts))

        log.debug("success", final_path=final_path)

        return final_path

    async def download_stream(self, stream_url: str, download_path: str):
        log = logger.bind(
            action="download_stream", stream_url=stream_url, download_path=download_path
        )

        if self.download_mode == DownloadMode.YTDLP:
            await self._download_ytdlp_async(stream_url, download_path)

        if self.download_mode == DownloadMode.NM3U8DLRE:
            await self._download_nm3u8dlre(stream_url, download_path)

        log.debug("success")

    async def _download_ytdlp_async(self, stream_url: str, download_path: str) -> None:
        await asyncio.to_thread(
            self._download_ytdlp_sync,
            stream_url,
            download_path,
        )

    def _download_ytdlp_sync(self, stream_url: str, download_path: str) -> None:
        with YoutubeDL(
            {
                "quiet": True,
                "no_warnings": True,
                "outtmpl": download_path,
                "allow_unplayable_formats": True,
                "overwrites": True,
                "fixup": "never",
                "noprogress": self.silent,
                "allowed_extractors": ["generic"],
            }
        ) as ydl:
            ydl.download(stream_url)

    async def _download_nm3u8dlre(self, stream_url: str, download_path: str):
        download_path_obj = Path(download_path)

        download_path_obj.parent.mkdir(parents=True, exist_ok=True)
        await async_subprocess(
            self.full_nm3u8dlre_path,
            stream_url,
            "--binary-merge",
            "--no-log",
            "--log-level",
            "off",
            "--ffmpeg-binary-path",
            self.full_ffmpeg_path,
            "--save-name",
            download_path_obj.stem,
            "--save-dir",
            download_path_obj.parent,
            "--tmp-dir",
            download_path_obj.parent,
            silent=self.silent,
        )

    async def apply_tags(
        self,
        media_path: str,
        tags: MediaTags,
        cover_bytes: bytes | None,
    ):
        log = logger.bind(action="apply_tags", media_path=media_path)

        exclude_tags = self.exclude_tags or []

        filtered_tags = MediaTags(
            **{
                k: v
                for k, v in tags.__dict__.items()
                if v is not None and k not in exclude_tags
            }
        )
        mp4_tags = filtered_tags.as_mp4_tags(self.date_tag_template)

        skip_tagging = "all" in exclude_tags

        await asyncio.to_thread(
            self._apply_mp4_tags,
            media_path,
            mp4_tags,
            cover_bytes,
            skip_tagging,
        )

        log.debug("success")

    def _apply_mp4_tags(
        self,
        media_path: str,
        tags: dict,
        cover_bytes: bytes | None,
        skip_tagging: bool,
    ):
        mp4 = MP4(media_path)
        mp4.clear()

        if not skip_tagging:
            if cover_bytes is not None:
                mp4["covr"] = [
                    MP4Cover(
                        data=cover_bytes,
                        imageformat=(
                            MP4Cover.FORMAT_JPEG
                            if self.interface.base.cover_format == CoverFormat.JPG
                            else MP4Cover.FORMAT_PNG
                        ),
                    )
                ]
            mp4.update(tags)

        mp4.save()

    async def _apply_cover(
        self,
        mp4: MP4,
        cover_bytes: bytes | None,
    ) -> None:
        if cover_bytes is None:
            return

        mp4["covr"] = [
            MP4Cover(
                data=cover_bytes,
                imageformat=(
                    MP4Cover.FORMAT_JPEG
                    if self.interface.base.cover_format == CoverFormat.JPG
                    else MP4Cover.FORMAT_PNG
                ),
            )
        ]

    def get_playlist_file_path(
        self,
        tags: PlaylistTags,
    ) -> str:
        log = logger.bind(action="get_playlist_file_path")

        template_folder_parts = self.playlist_folder_template.split("/")
        template_file_parts = self.playlist_file_template.split("/")
        template_parts = template_folder_parts + template_file_parts
        formatted_parts = []

        for i, part in enumerate(template_parts):
            is_folder = i < len(template_parts) - 1
            formatted_part = CustomStringFormatter().format(
                part,
                playlist_artist=(tags.artist, "Unknown Playlist Artist"),
                playlist_id=(tags.playlist_id, "Unknown Playlist ID"),
                playlist_title=(tags.title, "Unknown Playlist Title"),
                playlist_track=(tags.track, ""),
            )
            file_ext = None if is_folder else ".m3u"
            sanitized_formatted_part = self._sanitize_string(
                formatted_part,
                file_ext,
            )
            formatted_parts.append(sanitized_formatted_part)

        final_path = str(Path(self.output_path, *formatted_parts))

        log.debug("success", playlist_file_path=final_path)

        return final_path
