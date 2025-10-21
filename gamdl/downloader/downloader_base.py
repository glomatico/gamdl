import asyncio
import re
import shutil
import uuid
from io import BytesIO
from pathlib import Path

import httpx
from async_lru import alru_cache
from mutagen.mp4 import MP4, MP4Cover
from PIL import Image
from pywidevine import Cdm, Device
from yt_dlp import YoutubeDL

from ..api.apple_music_api import AppleMusicApi
from ..interface.interface import AppleMusicInterface
from ..interface.types import MediaTags, PlaylistTags
from ..utils import async_subprocess, raise_for_status
from .constants import (
    ILLEGAL_CHAR_REPLACEMENT,
    ILLEGAL_CHARS_RE,
    IMAGE_FILE_EXTENSION_MAP,
    TEMP_PATH_TEMPLATE,
)
from .enums import CoverFormat, DownloadMode, RemuxMode
from .hardcoded_wvd import HARDCODED_WVD


class AppleMusicBaseDownloader:
    def __init__(
        self,
        api: AppleMusicApi,
        output_path: str = "./Apple Music",
        temp_path: str = ".",
        wvd_path: str = None,
        overwrite: bool = False,
        save_cover: bool = False,
        save_playlist: bool = False,
        no_synced_lyrics: bool = False,
        synced_lyrics_only: bool = False,
        nm3u8dlre_path: str = "N_m3u8DL-RE",
        mp4decrypt_path: str = "mp4decrypt",
        ffmpeg_path: str = "ffmpeg",
        mp4box_path: str = "MP4Box",
        download_mode: DownloadMode = DownloadMode.YTDLP,
        remux_mode: RemuxMode = RemuxMode.FFMPEG,
        cover_format: CoverFormat = CoverFormat.JPG,
        template_folder_album: str = "{album_artist}/{album}",
        template_folder_compilation: str = "Compilations/{album}",
        template_file_single_disc: str = "{track:02d} {title}",
        template_file_multi_disc: str = "{disc}-{track:02d} {title}",
        template_folder_no_album: str = "{artist}/Unknown Album",
        template_file_no_album: str = "{title}",
        template_file_playlist: str = "Playlists/{playlist_artist}/{playlist_title}",
        template_date: str = "%Y-%m-%dT%H:%M:%SZ",
        exclude_tags: list[str] = None,
        cover_size: int = 1200,
        truncate: int = None,
        database_path: str = None,
        silent: bool = False,
        skip_processing: bool = False,
    ):
        self.api = api
        self.output_path = output_path
        self.temp_path = temp_path
        self.wvd_path = wvd_path
        self.overwrite = overwrite
        self.save_cover = save_cover
        self.save_playlist = save_playlist
        self.no_synced_lyrics = no_synced_lyrics
        self.synced_lyrics_only = synced_lyrics_only
        self.nm3u8dlre_path = nm3u8dlre_path
        self.mp4decrypt_path = mp4decrypt_path
        self.ffmpeg_path = ffmpeg_path
        self.mp4box_path = mp4box_path
        self.download_mode = download_mode
        self.remux_mode = remux_mode
        self.cover_format = cover_format
        self.template_folder_album = template_folder_album
        self.template_folder_compilation = template_folder_compilation
        self.template_file_single_disc = template_file_single_disc
        self.template_file_multi_disc = template_file_multi_disc
        self.template_folder_no_album = template_folder_no_album
        self.template_file_no_album = template_file_no_album
        self.template_file_playlist = template_file_playlist
        self.template_date = template_date
        self.exclude_tags = exclude_tags
        self.cover_size = cover_size
        self.truncate = truncate
        self.database_path = database_path
        self.silent = silent
        self.skip_processing = skip_processing

    def setup(self):
        self._setup_binary_paths()
        self._setup_cdm()
        self._setup_interface()

    def _setup_binary_paths(self):
        self.full_n3u8dlre_path = shutil.which(self.nm3u8dlre_path)
        self.full_mp4decrypt_path = shutil.which(self.mp4decrypt_path)
        self.full_ffmpeg_path = shutil.which(self.ffmpeg_path)
        self.full_mp4box_path = shutil.which(self.mp4box_path)

    def _setup_cdm(self):
        if self.wvd_path:
            self.cdm = Cdm.from_device(Device.load(self.wvd_path))
        else:
            self.cdm = Cdm.from_device(Device.loads(HARDCODED_WVD))

    def _setup_interface(self):
        self.interface = AppleMusicInterface(self.api)

    def get_random_uuid(self) -> str:
        return uuid.uuid4().hex[:8]

    def is_media_streamable(
        self,
        media_metadata: dict,
    ) -> bool:
        return bool(media_metadata["attributes"].get("playParams"))

    async def get_cover_file_extension(self, cover_url_template: str) -> str | None:
        if self.cover_format != CoverFormat.RAW:
            return f".{self.cover_format.value}"

        cover_url = self.get_cover_url(cover_url_template)
        cover_bytes = await self.get_cover_bytes(cover_url)
        if cover_bytes is None:
            return None

        image_obj = Image.open(BytesIO(self.get_cover_bytes(cover_url)))
        image_format = image_obj.format.lower()
        return IMAGE_FILE_EXTENSION_MAP.get(
            image_format,
            f".{image_format.lower()}",
        )

    def get_playlist_tags(
        self,
        playlist_metadata: dict,
        media_metadata: dict,
    ) -> PlaylistTags:
        playlist_track = (
            playlist_metadata["relationships"]["tracks"]["data"].index(media_metadata)
            + 1
        )

        return PlaylistTags(
            playlist_artist=playlist_metadata["attributes"].get(
                "curatorName", "Unknown"
            ),
            playlist_id=playlist_metadata["attributes"]["playParams"]["id"],
            playlist_title=playlist_metadata["attributes"]["name"],
            playlist_track=playlist_track,
        )

    def get_temp_path(
        self,
        media_id: str,
        folder_tag: str,
        file_tag: str,
        file_extension: str,
    ) -> str:
        return str(
            Path(self.temp_path)
            / TEMP_PATH_TEMPLATE.format(folder_tag)
            / (f"{media_id}_{file_tag}" + file_extension)
        )

    @alru_cache()
    async def get_cover_bytes(self, cover_url: str) -> bytes | None:
        async with httpx.AsyncClient() as client:
            response = await client.get(cover_url)
            raise_for_status(response, {200, 404})

            if response.status_code == 200:
                return response.content
            return None

    def get_sanitized_string(self, dirty_string: str, is_folder: bool) -> str:
        dirty_string = re.sub(
            ILLEGAL_CHARS_RE,
            ILLEGAL_CHAR_REPLACEMENT,
            dirty_string,
        )
        if is_folder:
            dirty_string = dirty_string[: self.truncate]
            if dirty_string.endswith("."):
                dirty_string = dirty_string[:-1] + ILLEGAL_CHAR_REPLACEMENT
        else:
            if self.truncate is not None:
                dirty_string = dirty_string[: self.truncate - 4]
        return dirty_string.strip()

    def get_final_path(
        self,
        tags: MediaTags,
        file_extension: str,
        playlist_tags: PlaylistTags,
    ) -> str:
        if tags.album is not None:
            template_folder = (
                self.template_folder_compilation.split("/")
                if tags.compilation
                else self.template_folder_album.split("/")
            )
            template_file = (
                self.template_file_multi_disc.split("/")
                if tags.disc_total > 1
                else self.template_file_single_disc.split("/")
            )
        else:
            template_folder = self.template_folder_no_album.split("/")
            template_file = self.template_file_no_album.split("/")

        template_final = template_folder + template_file

        tags_dict = tags.__dict__.copy()
        if playlist_tags:
            tags_dict.update(playlist_tags.__dict__)

        return str(
            Path(
                self.output_path,
                *[
                    self.get_sanitized_string(i.format(**tags_dict), True)
                    for i in template_final[0:-1]
                ],
                (
                    self.get_sanitized_string(
                        template_final[-1].format(**tags_dict), False
                    )
                    + file_extension
                ),
            )
        )

    def get_cover_url_template(self, metadata: dict) -> str:
        if self.cover_format == CoverFormat.RAW:
            return self._get_raw_cover_url(metadata["attributes"]["artwork"]["url"])
        return metadata["attributes"]["artwork"]["url"]

    def _get_raw_cover_url(self, cover_url_template: str) -> str:
        return re.sub(
            r"image/thumb/",
            "",
            re.sub(
                r"is1-ssl",
                "a1",
                cover_url_template,
            ),
        )

    def get_cover_url(self, cover_url_template: str) -> str:
        return re.sub(
            r"\{w\}x\{h\}([a-z]{2})\.jpg",
            (
                f"{self.cover_size}x{self.cover_size}bb.{self.cover_format.value}"
                if self.cover_format != CoverFormat.RAW
                else ""
            ),
            cover_url_template,
        )

    async def download_stream(self, stream_url: str, download_path: str):
        if self.download_mode == DownloadMode.YTDLP:
            await self.download_ytdlp(stream_url, download_path)

        if self.download_mode == DownloadMode.NM3U8DLRE:
            await self.download_nm3u8dlre(stream_url, download_path)

    async def download_ytdlp(self, stream_url: str, download_path: str) -> None:
        await asyncio.to_thread(
            self._download_ytdlp,
            stream_url,
            download_path,
        )

    def _download_ytdlp(self, stream_url: str, download_path: str) -> None:
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

    async def download_nm3u8dlre(self, stream_url: str, download_path: str):
        download_path_obj = Path(download_path)

        download_path_obj.parent.mkdir(parents=True, exist_ok=True)
        await async_subprocess(
            self.full_n3u8dlre_path,
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
        media_path: Path,
        tags: MediaTags,
        cover_url_template: str,
    ):
        exclude_tags = self.exclude_tags or []

        filtered_tags = MediaTags(
            **{
                k: v
                for k, v in tags.__dict__.items()
                if v is not None and k not in exclude_tags
            }
        )
        mp4_tags = filtered_tags.as_mp4_tags(self.template_date)
        skip_tagging = "all" in exclude_tags

        mp4 = MP4(media_path)
        mp4.clear()

        if not skip_tagging:
            if "cover" not in exclude_tags and self.cover_format != CoverFormat.RAW:
                await self._apply_cover(mp4, cover_url_template)
            mp4.update(mp4_tags)

        mp4.save()

    async def _apply_cover(
        self,
        mp4: MP4,
        cover_url_template: str,
    ) -> None:
        cover_url = self.get_cover_url(cover_url_template)
        cover_bytes = await self.get_cover_bytes(cover_url)
        if cover_bytes is None:
            return

        mp4["covr"] = [
            MP4Cover(
                data=cover_bytes,
                imageformat=(
                    MP4Cover.FORMAT_JPEG
                    if self.cover_format == CoverFormat.JPG
                    else MP4Cover.FORMAT_PNG
                ),
            )
        ]

    def move_to_final_path(self, stage_path: str, final_path: str) -> None:
        Path(final_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(stage_path, final_path)

    def write_cover_image(
        self,
        cover_bytes: bytes,
        cover_path: str,
    ) -> None:
        Path(cover_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cover_path).write_bytes(cover_bytes)

    def cleanup_temp(self, random_uuid: str) -> None:
        temp_folder = Path(self.temp_path) / TEMP_PATH_TEMPLATE.format(random_uuid)
        if temp_folder.exists():
            shutil.rmtree(temp_folder)
