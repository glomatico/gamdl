from __future__ import annotations

import base64
import datetime
import functools
import io
import logging
import re
import shutil
import subprocess
import typing
import urllib.parse
import uuid
from pathlib import Path

import colorama
import requests
from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from mutagen.mp4 import MP4, MP4Cover
from PIL import Image
from pywidevine import PSSH, Cdm, Device
from yt_dlp import YoutubeDL

from .apple_music_api import AppleMusicApi
from .database import Database
from .enums import CoverFormat, DownloadMode, MediaFileFormat, RemuxMode
from .hardcoded_wvd import HARDCODED_WVD
from .itunes_api import ItunesApi
from .models import (
    DecryptionKey,
    DownloadInfo,
    DownloadQueue,
    MediaTags,
    PlaylistTags,
    UrlInfo,
)
from .utils import color_text, raise_response_exception

logger = logging.getLogger("gamdl")


class Downloader:
    ILLEGAL_CHARS_RE = r'[\\/:*?"<>|;]'
    ILLEGAL_CHAR_REPLACEMENT = "_"
    VALID_URL_RE = (
        r"("
        r"/(?P<storefront>[a-z]{2})"
        r"/(?P<type>artist|album|playlist|song|music-video|post)"
        r"(?:/(?P<slug>[^\s/]+))?"
        r"/(?P<id>[0-9]+|pl\.[0-9a-z]{32}|pl\.u-[a-zA-Z0-9]+)"
        r"(?:\?i=(?P<sub_id>[0-9]+))?"
        r")|("
        r"(?:/(?P<library_storefront>[a-z]{2}))?"
        r"/library/(?P<library_type>|playlist|albums)"
        r"/(?P<library_id>p\.[a-zA-Z0-9]{15}|l\.[a-zA-Z0-9]{7})"
        r")"
    )
    IMAGE_FILE_EXTENSION_MAP = {
        "jpeg": ".jpg",
        "tiff": ".tif",
    }

    def __init__(
        self,
        apple_music_api: AppleMusicApi,
        itunes_api: ItunesApi,
        output_path: Path = Path("./Apple Music"),
        temp_path: Path = Path("."),
        wvd_path: Path = None,
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
        database_path: Path = None,
        silent: bool = False,
        skip_processing: bool = False,
    ):
        self.apple_music_api = apple_music_api
        self.itunes_api = itunes_api
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
        self._set_temp_path()
        self._set_exclude_tags()
        self._set_binaries_path_full()
        self._set_truncate()
        self._set_database()
        self._set_subprocess_additional_args()

    def _set_temp_path(self):
        random_suffix = uuid.uuid4().hex[:8]
        self.temp_path_generated = self.temp_path / f"gamdl_temp_{random_suffix}"

    def _set_exclude_tags(self):
        self.exclude_tags = self.exclude_tags if self.exclude_tags is not None else []

    def _set_binaries_path_full(self):
        self.nm3u8dlre_path_full = shutil.which(self.nm3u8dlre_path)
        self.ffmpeg_path_full = shutil.which(self.ffmpeg_path)
        self.mp4box_path_full = shutil.which(self.mp4box_path)
        self.mp4decrypt_path_full = shutil.which(self.mp4decrypt_path)

    def _set_truncate(self):
        if self.truncate is not None:
            self.truncate = None if self.truncate < 4 else self.truncate

    def _set_database(self):
        if self.database_path is not None:
            self.database = Database(self.database_path)
        else:
            self.database = None

    def _set_subprocess_additional_args(self):
        if self.silent:
            self.subprocess_additional_args = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
        else:
            self.subprocess_additional_args = {}

    def set_cdm(self):
        if self.wvd_path:
            self.cdm = Cdm.from_device(Device.load(self.wvd_path))
        else:
            self.cdm = Cdm.from_device(Device.loads(HARDCODED_WVD))

    def parse_url_info(self, url: str) -> UrlInfo | None:
        url = urllib.parse.unquote(url)

        url_regex_result = re.search(
            self.VALID_URL_RE,
            url,
        )
        if not url_regex_result:
            return None

        return UrlInfo(
            **url_regex_result.groupdict(),
        )

    def get_download_queue(self, url_info: UrlInfo) -> DownloadQueue:
        return self._get_download_queue(
            "song" if url_info.sub_id else url_info.type,
            url_info.sub_id or url_info.id or url_info.library_id,
            url_info.library_id is not None,
        )

    def _get_download_queue(
        self,
        url_type: str,
        id: str,
        is_library: bool,
    ) -> DownloadQueue | None:
        download_queue = DownloadQueue()

        if url_type == "artist":
            artist = self.apple_music_api.get_artist(id)

            if artist is None:
                return None

            download_queue.medias_metadata = list(
                self.get_download_queue_from_artist(artist)
            )

        if url_type == "song":
            song = self.apple_music_api.get_song(id)

            if song is None:
                return None

            download_queue.medias_metadata = [song]

        if url_type in {"album", "albums"}:
            if is_library:
                album = self.apple_music_api.get_library_album(id)
            else:
                album = self.apple_music_api.get_album(id)

            if album is None:
                return None

            download_queue.medias_metadata = [
                track for track in album["relationships"]["tracks"]["data"]
            ]

        if url_type == "playlist":
            if is_library:
                playlist = self.apple_music_api.get_library_playlist(id)
            else:
                playlist = self.apple_music_api.get_playlist(id)

            if playlist is None:
                return None

            download_queue.medias_metadata = [
                track for track in playlist["relationships"]["tracks"]["data"]
            ]
            download_queue.playlist_attributes = playlist["attributes"]

        if url_type == "music-video":
            music_video = self.apple_music_api.get_music_video(id)

            if music_video is None:
                return None

            download_queue.medias_metadata = [music_video]

        if url_type == "post":
            post = self.apple_music_api.get_post(id)

            if post is None:
                return None

            download_queue.medias_metadata = [post]

        return download_queue

    def get_download_queue_from_artist(
        self,
        artist: dict,
    ) -> typing.Generator[dict, None, None]:
        media_type = inquirer.select(
            message=f'Select which type to download for artist "{artist["attributes"]["name"]}":',
            choices=[
                Choice(name="Albums", value="albums"),
                Choice(
                    name="Music Videos",
                    value="music-videos",
                ),
            ],
            validate=lambda result: artist["relationships"].get(result, {}).get("data"),
            invalid_message="The artist doesn't have any items of this type",
        ).execute()
        if media_type == "albums":
            yield from self.select_albums_from_artist(
                artist["relationships"]["albums"]["data"]
            )
        elif media_type == "music-videos":
            yield from self.select_music_videos_from_artist(
                artist["relationships"]["music-videos"]["data"]
            )

    def select_albums_from_artist(
        self,
        albums: list[dict],
    ) -> typing.Generator[dict, None, None]:
        choices = [
            Choice(
                name=" | ".join(
                    [
                        f'{album["attributes"]["trackCount"]:03d}',
                        f'{album["attributes"]["releaseDate"]:<10}',
                        f'{album["attributes"].get("contentRating", "None").title():<8}',
                        f'{album["attributes"]["name"]}',
                    ]
                ),
                value=album,
            )
            for album in albums
        ]
        selected = inquirer.select(
            message="Select which albums to download: (Track Count | Release Date | Rating | Title)",
            choices=choices,
            multiselect=True,
        ).execute()
        for album in selected:
            for track in self.apple_music_api.get_album(album["id"])["relationships"][
                "tracks"
            ]["data"]:
                yield track

    def select_music_videos_from_artist(
        self,
        music_videos: list[dict],
    ) -> typing.Generator[dict, None, None]:
        choices = [
            Choice(
                name=" | ".join(
                    [
                        self.millis_to_min_sec(
                            music_video["attributes"]["durationInMillis"]
                        ),
                        f'{music_video["attributes"].get("contentRating", "None").title():<8}',
                        music_video["attributes"]["name"],
                    ],
                ),
                value=music_video,
            )
            for music_video in music_videos
        ]
        selected = inquirer.select(
            message="Select which music videos to download: (Duration | Rating | Title)",
            choices=choices,
            multiselect=True,
        ).execute()
        for music_video in selected:
            yield music_video

    def get_media_id_of_library_media(
        self,
        library_media_metadata: dict,
    ) -> str:
        play_params = library_media_metadata["attributes"].get("playParams", {})
        return play_params.get("catalogId", library_media_metadata["id"])

    def is_media_streamable(
        self,
        media_metadata: dict,
    ) -> bool:
        return bool(media_metadata["attributes"].get("playParams"))

    def get_database_final_path(self, media_id: str) -> Path | None:
        if self.database is None:
            return

        final_path_database = self.database.get_media(media_id)
        if (
            final_path_database is not None
            and final_path_database.exists()
            and not self.overwrite
        ):
            return final_path_database

    def get_playlist_tags(
        self,
        playlist_attributes: dict,
        playlist_track: int,
    ) -> PlaylistTags:
        return PlaylistTags(
            playlist_artist=playlist_attributes.get("curatorName", "Unknown"),
            playlist_id=playlist_attributes["playParams"]["id"],
            playlist_title=playlist_attributes["name"],
            playlist_track=playlist_track,
        )

    def get_playlist_file_path(
        self,
        tags: PlaylistTags,
    ) -> Path:
        template_file = self.template_file_playlist.split("/")
        tags_dict = tags.__dict__.copy()

        return Path(
            self.output_path,
            *[
                self.get_sanitized_string(i.format(**tags_dict), True)
                for i in template_file[0:-1]
            ],
            *[
                self.get_sanitized_string(template_file[-1].format(**tags_dict), False)
                + ".m3u8"
            ],
        )

    def update_playlist_file(
        self,
        playlist_file_path: Path,
        final_path: Path,
        playlist_track: int,
    ):
        playlist_file_path.parent.mkdir(parents=True, exist_ok=True)
        playlist_file_path_parent_parts_len = len(playlist_file_path.parent.parts)
        output_path_parts_len = len(self.output_path.parts)
        final_path_relative = Path(
            ("../" * (playlist_file_path_parent_parts_len - output_path_parts_len)),
            *final_path.parts[output_path_parts_len:],
        )
        playlist_file_lines = (
            playlist_file_path.open("r", encoding="utf8").readlines()
            if playlist_file_path.exists()
            else []
        )
        if len(playlist_file_lines) < playlist_track:
            playlist_file_lines.extend(
                "\n" for _ in range(playlist_track - len(playlist_file_lines))
            )
        playlist_file_lines[playlist_track - 1] = final_path_relative.as_posix() + "\n"
        with playlist_file_path.open("w", encoding="utf8") as playlist_file:
            playlist_file.writelines(playlist_file_lines)

    @staticmethod
    def millis_to_min_sec(millis) -> str:
        minutes, seconds = divmod(millis // 1000, 60)
        return f"{minutes:02d}:{seconds:02d}"

    def parse_date(self, date: str) -> datetime.datetime:
        return datetime.datetime.fromisoformat(date.split("Z")[0])

    def get_decryption_key(self, pssh: str, track_id: str) -> DecryptionKey:
        try:
            cdm_session = self.cdm.open()

            pssh_obj = PSSH(pssh.split(",")[-1])

            challenge = base64.b64encode(
                self.cdm.get_license_challenge(cdm_session, pssh_obj)
            ).decode()
            license = self.apple_music_api.get_widevine_license(
                track_id,
                pssh,
                challenge,
            )

            self.cdm.parse_license(cdm_session, license)
            decryption_key_info = next(
                i for i in self.cdm.get_keys(cdm_session) if i.type == "CONTENT"
            )
        finally:
            self.cdm.close(cdm_session)
        return DecryptionKey(
            key=decryption_key_info.key.hex(),
            kid=decryption_key_info.kid.hex,
        )

    def download(self, path: Path, stream_url: str):
        if self.download_mode == DownloadMode.YTDLP:
            self.download_ytdlp(path, stream_url)
        elif self.download_mode == DownloadMode.NM3U8DLRE:
            self.download_nm3u8dlre(path, stream_url)

    def download_ytdlp(self, path: Path, stream_url: str):
        with YoutubeDL(
            {
                "quiet": True,
                "no_warnings": True,
                "outtmpl": str(path),
                "allow_unplayable_formats": True,
                "fixup": "never",
                "allowed_extractors": ["generic"],
                "noprogress": self.silent,
            }
        ) as ydl:
            ydl.download(stream_url)

    def download_nm3u8dlre(self, path: Path, stream_url: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                self.nm3u8dlre_path_full,
                stream_url,
                "--binary-merge",
                "--no-log",
                "--log-level",
                "off",
                "--ffmpeg-binary-path",
                self.ffmpeg_path_full,
                "--save-name",
                path.stem,
                "--save-dir",
                path.parent,
                "--tmp-dir",
                path.parent,
            ],
            check=True,
            **self.subprocess_additional_args,
        )

    def get_sanitized_string(self, dirty_string: str, is_folder: bool) -> str:
        dirty_string = re.sub(
            self.ILLEGAL_CHARS_RE,
            self.ILLEGAL_CHAR_REPLACEMENT,
            dirty_string,
        )
        if is_folder:
            dirty_string = dirty_string[: self.truncate]
            if dirty_string.endswith("."):
                dirty_string = dirty_string[:-1] + self.ILLEGAL_CHAR_REPLACEMENT
        else:
            if self.truncate is not None:
                dirty_string = dirty_string[: self.truncate - 4]
        return dirty_string.strip()

    def get_media_file_extension(
        self,
        media_file_format: MediaFileFormat,
    ) -> str:
        return "." + media_file_format.value

    def get_temp_path(
        self,
        media_id: str,
        tag: str,
        file_extension: str,
    ):
        temp_path = self.temp_path_generated / (f"{media_id}_{tag}" + file_extension)
        return temp_path

    def get_final_path(
        self,
        tags: MediaTags,
        file_extension: str,
        playlist_tags: PlaylistTags,
    ) -> Path:
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

        return Path(
            self.output_path,
            *[
                self.get_sanitized_string(i.format(**tags_dict), True)
                for i in template_final[0:-1]
            ],
            (
                self.get_sanitized_string(template_final[-1].format(**tags_dict), False)
                + file_extension
            ),
        )

    def get_cover_format(self, cover_url: str) -> str | None:
        cover_bytes = self.get_cover_bytes(cover_url)
        if cover_bytes is None:
            return None
        image_obj = Image.open(io.BytesIO(self.get_cover_bytes(cover_url)))
        image_format = image_obj.format.lower()
        return image_format

    def get_cover_file_extension(self, cover_format: str) -> str:
        return self.IMAGE_FILE_EXTENSION_MAP.get(
            cover_format,
            f".{cover_format.lower()}",
        )

    def get_cover_url(self, metadata: dict) -> str:
        if self.cover_format == CoverFormat.RAW:
            return self._get_raw_cover_url(metadata["attributes"]["artwork"]["url"])
        return self._get_cover_url(metadata["attributes"]["artwork"]["url"])

    def _get_raw_cover_url(self, cover_url_template: str) -> str:
        return re.sub(
            r"image/thumb/",
            "",
            re.sub(
                r"is1-ssl",
                "a1",
                re.sub(
                    r"/\{w\}x\{h\}([a-z]{2})\.jpg",
                    "",
                    cover_url_template,
                ),
            ),
        )

    def _get_cover_url(self, cover_url_template: str) -> str:
        return re.sub(
            r"\{w\}x\{h\}([a-z]{2})\.jpg",
            f"{self.cover_size}x{self.cover_size}bb.{self.cover_format.value}",
            cover_url_template,
        )

    @staticmethod
    @functools.lru_cache()
    def get_cover_bytes(url: str) -> bytes | None:
        response = requests.get(url)
        if response.status_code == 200:
            return response.content
        elif response.status_code in (404, 400):
            return None
        else:
            raise_response_exception(response)
        return response.content

    def apply_tags(
        self,
        path: Path,
        tags: MediaTags,
        cover_url: str,
    ):
        filtered_tags = MediaTags(
            **{
                k: v
                for k, v in tags.__dict__.items()
                if v is not None and k not in self.exclude_tags
            }
        )
        mp4_tags = filtered_tags.to_mp4_tags(self.template_date)
        skip_tagging = "all" in self.exclude_tags

        mp4 = MP4(path)
        mp4.clear()
        if not skip_tagging:
            if (
                "cover" not in self.exclude_tags
                and self.cover_format != CoverFormat.RAW
            ):
                self._apply_cover(mp4, cover_url)
            mp4.update(mp4_tags)
        mp4.save()

    def _apply_cover(
        self,
        mp4: MP4,
        cover_url: str,
    ) -> None:
        cover_bytes = self.get_cover_bytes(cover_url)
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

    def move_to_output_path(
        self,
        staged_path: Path,
        final_path: Path,
    ):
        final_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(staged_path, final_path)

    @functools.lru_cache()
    def write_cover(self, cover_path: Path, cover_url: str):
        cover_path.parent.mkdir(parents=True, exist_ok=True)
        cover_path.write_bytes(self.get_cover_bytes(cover_url))

    def write_synced_lyrics(
        self,
        synced_lyrics_path: Path,
        synced_lyrics: str,
    ):
        synced_lyrics_path.parent.mkdir(parents=True, exist_ok=True)
        synced_lyrics_path.write_text(
            synced_lyrics,
            encoding="utf8",
        )

    def cleanup_temp_path(self) -> None:
        if self.temp_path_generated.exists():
            shutil.rmtree(self.temp_path_generated)

    def _final_processing_wrapper(
        self,
        func,
        *args,
        **kwargs,
    ) -> typing.Generator[DownloadInfo, None, None]:
        exception = None
        download_info = None
        try:
            for download_info in func(*args, **kwargs):
                yield download_info
        except Exception as e:
            exception = e
        finally:
            if download_info is not None and isinstance(download_info, DownloadInfo):
                self._final_processing(
                    download_info,
                )

            if exception is not None:
                raise exception

    def _final_processing(
        self,
        download_info: DownloadInfo,
    ) -> None:
        if self.skip_processing:
            return

        if download_info.media_id:
            colored_media_id = color_text(
                download_info.media_id,
                colorama.Style.DIM,
            )
        else:
            colored_media_id = color_text(
                "Unknown",
                colorama.Style.DIM,
            )

        if download_info.staged_path:
            logger.debug(
                f'[{colored_media_id}] Applying tags to "{download_info.staged_path}"'
            )
            self.apply_tags(
                download_info.staged_path,
                download_info.tags,
                download_info.cover_url,
            )
            logger.debug(
                f'[{colored_media_id}] Moving "{download_info.staged_path}" to "{download_info.final_path}"'
            )
            self.move_to_output_path(
                download_info.staged_path,
                download_info.final_path,
            )
            logger.info(f"[{colored_media_id}] Download completed successfully")

            if self.database is not None:
                logger.debug(
                    f'[{colored_media_id}] Adding entry to database at "{self.database_path}"'
                )
                self.database.add_media(
                    download_info.media_id,
                    download_info.final_path,
                )

        if (
            download_info.cover_path and not self.save_cover
        ) or not download_info.cover_path:
            pass
        elif download_info.cover_path.exists() and not self.overwrite:
            logger.debug(
                f'[{colored_media_id}] Cover already exists at "{download_info.cover_path}", skipping'
            )
        else:
            logger.debug(
                f'[{colored_media_id}] Saving cover to "{download_info.cover_path}"'
            )
            self.write_cover(
                download_info.cover_path,
                download_info.cover_url,
            )

        if (
            self.no_synced_lyrics
            or not download_info.lyrics
            or not download_info.lyrics.synced
        ):
            pass
        elif download_info.synced_lyrics_path.exists() and not self.overwrite:
            logger.debug(
                f'[{colored_media_id}] Synced lyrics already exist at "{download_info.synced_lyrics_path}", skipping'
            )
        else:
            logger.debug(
                f'[{colored_media_id}] Saving synced lyrics to "{download_info.synced_lyrics_path}"'
            )
            self.write_synced_lyrics(
                download_info.synced_lyrics_path,
                download_info.lyrics.synced,
            )

        if download_info.playlist_tags and self.save_playlist:
            playlist_file_path = self.get_playlist_file_path(
                download_info.playlist_tags
            )
            logger.debug(
                f'[{colored_media_id}] Updating playlist file "{playlist_file_path}"'
            )
            self.update_playlist_file(
                playlist_file_path,
                download_info.final_path,
                download_info.playlist_tags.playlist_track,
            )

        self.cleanup_temp_path()
