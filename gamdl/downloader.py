from __future__ import annotations

import base64
import datetime
import functools
import io
import re
import shutil
import subprocess
import typing
from pathlib import Path

import requests
from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from mutagen.mp4 import MP4, MP4Cover
from PIL import Image
from pywidevine import PSSH, Cdm, Device
from yt_dlp import YoutubeDL

from .apple_music_api import AppleMusicApi
from .constants import IMAGE_FILE_EXTENSION_MAP, MP4_TAGS_MAP
from .enums import CoverFormat, DownloadMode, MediaFileFormat, RemuxMode
from .hardcoded_wvd import HARDCODED_WVD
from .itunes_api import ItunesApi
from .models import DownloadQueue, UrlInfo
from .utils import raise_response_exception


class Downloader:
    ILLEGAL_CHARS_RE = r'[\\/:*?"<>|;]'
    ILLEGAL_CHAR_REPLACEMENT = "_"
    VALID_URL_RE = (
        r"(/(?P<storefront>[a-z]{2})/(?P<type>artist|album|playlist|song|music-video|post)/(?P<slug>[^/]*)(?:/(?P<id>[^/?]*))?(?:\?i=)?(?P<sub_id>[0-9a-z]*)?)|"
        r"(/library/(?P<library_type>|playlist|albums)/(?P<library_id>[a-z]\.[0-9a-zA-Z]*))"
    )

    def __init__(
        self,
        apple_music_api: AppleMusicApi,
        itunes_api: ItunesApi,
        output_path: Path = Path("./Apple Music"),
        temp_path: Path = Path("./temp"),
        wvd_path: Path = None,
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
        exclude_tags: str = None,
        cover_size: int = 1200,
        truncate: int = None,
        silent: bool = False,
    ):
        self.apple_music_api = apple_music_api
        self.itunes_api = itunes_api
        self.output_path = output_path
        self.temp_path = temp_path
        self.wvd_path = wvd_path
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
        self.silent = silent
        self._set_binaries_path_full()
        self._set_exclude_tags_list()
        self._set_truncate()
        self._set_subprocess_additional_args()

    def _set_binaries_path_full(self):
        self.nm3u8dlre_path_full = shutil.which(self.nm3u8dlre_path)
        self.ffmpeg_path_full = shutil.which(self.ffmpeg_path)
        self.mp4box_path_full = shutil.which(self.mp4box_path)
        self.mp4decrypt_path_full = shutil.which(self.mp4decrypt_path)

    def _set_exclude_tags_list(self):
        self.exclude_tags_list = (
            [i.lower() for i in self.exclude_tags.split(",")]
            if self.exclude_tags is not None
            else []
        )

    def _set_truncate(self):
        if self.truncate is not None:
            self.truncate = None if self.truncate < 4 else self.truncate

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

    def get_url_info(self, url: str) -> UrlInfo:
        url_info = UrlInfo()
        url_regex_result = re.search(
            self.VALID_URL_RE,
            url,
        )
        is_library = url_regex_result.group("library_type") is not None
        if is_library:
            url_info.type = url_regex_result.group("library_type")
            url_info.id = url_regex_result.group("library_id")
        else:
            url_info.storefront = url_regex_result.group("storefront")
            url_info.type = (
                "song"
                if url_regex_result.group("sub_id")
                else url_regex_result.group("type")
            )
            url_info.id = (
                url_regex_result.group("sub_id")
                or url_regex_result.group("id")
                or url_regex_result.group("sub_id")
            )
        url_info.is_library = is_library
        return url_info

    def get_download_queue(self, url_info: UrlInfo) -> DownloadQueue:
        return self._get_download_queue(url_info.type, url_info.id, url_info.is_library)

    def _get_download_queue(
        self,
        url_type: str,
        id: str,
        is_library: bool,
    ) -> DownloadQueue:
        download_queue = DownloadQueue()
        if url_type == "artist":
            artist = self.apple_music_api.get_artist(id)
            download_queue.medias_metadata = list(
                self.get_download_queue_from_artist(artist)
            )
        elif url_type == "song":
            download_queue.medias_metadata = [self.apple_music_api.get_song(id)]
        elif url_type in ("album", "albums"):
            if is_library:
                album = self.apple_music_api.get_library_album(id)
            else:
                album = self.apple_music_api.get_album(id)
            download_queue.medias_metadata = [
                track for track in album["relationships"]["tracks"]["data"]
            ]
        elif url_type == "playlist":
            if is_library:
                playlist = self.apple_music_api.get_library_playlist(id)
                download_queue.medias_metadata = [
                    track for track in playlist["relationships"]["tracks"]["data"]
                ]
            else:
                playlist = self.apple_music_api.get_playlist(id)
                download_queue.medias_metadata = [
                    track for track in playlist["relationships"]["tracks"]["data"]
                ]
            download_queue.playlist_attributes = playlist["attributes"]
        elif url_type == "music-video":
            download_queue.medias_metadata = [self.apple_music_api.get_music_video(id)]
        elif url_type == "post":
            download_queue.medias_metadata = [self.apple_music_api.get_post(id)]
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

    def get_media_id(
        self,
        media_metadata: dict,
    ) -> str | None:
        play_params = media_metadata["attributes"].get("playParams", {})
        return play_params.get("catalogId") or play_params.get("id")

    def get_playlist_tags(
        self,
        playlist_attributes: dict,
        playlist_track: int,
    ) -> dict:
        tags = {
            "playlist_artist": playlist_attributes.get("curatorName", "Apple Music"),
            "playlist_id": playlist_attributes["playParams"]["id"],
            "playlist_title": playlist_attributes["name"],
            "playlist_track": playlist_track,
        }
        return tags

    def get_playlist_file_path(
        self,
        tags: dict,
    ):
        template_file = self.template_file_playlist.split("/")
        return Path(
            self.output_path,
            *[
                self.get_sanitized_string(i.format(**tags), True)
                for i in template_file[0:-1]
            ],
            *[
                self.get_sanitized_string(template_file[-1].format(**tags), False)
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

    def sanitize_date(self, date: str) -> datetime.datetime:
        return datetime.datetime.fromisoformat(date[:-1]).strftime(self.template_date)

    def get_decryption_key(self, pssh: str, track_id: str) -> str:
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
            decryption_key = next(
                i for i in self.cdm.get_keys(cdm_session) if i.type == "CONTENT"
            ).key.hex()
        finally:
            self.cdm.close(cdm_session)
        return decryption_key

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

    def get_final_file_extension(
        self,
        file_format: MediaFileFormat,
    ) -> str:
        return "." + file_format.value

    def get_final_path(self, tags: dict, file_extension: str) -> Path:
        if tags.get("album"):
            template_folder = (
                self.template_folder_compilation.split("/")
                if tags.get("compilation")
                else self.template_folder_album.split("/")
            )
            template_file = (
                self.template_file_multi_disc.split("/")
                if tags["disc_total"] > 1
                else self.template_file_single_disc.split("/")
            )
        else:
            template_folder = self.template_folder_no_album.split("/")
            template_file = self.template_file_no_album.split("/")
        template_final = template_folder + template_file
        return Path(
            self.output_path,
            *[
                self.get_sanitized_string(i.format(**tags), True)
                for i in template_final[0:-1]
            ],
            (
                self.get_sanitized_string(template_final[-1].format(**tags), False)
                + file_extension
            ),
        )

    def get_cover_file_extension(self, cover_url: str) -> str | None:
        cover_bytes = self.get_cover_url_response_bytes(cover_url)
        if cover_bytes is None:
            return None
        image_obj = Image.open(io.BytesIO(self.get_cover_url_response_bytes(cover_url)))
        image_format = image_obj.format.lower()
        return IMAGE_FILE_EXTENSION_MAP.get(image_format, f".{image_format}")

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
    def get_cover_url_response_bytes(url: str) -> bytes | None:
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
        tags: dict,
        cover_url: str,
    ):
        to_apply_tags = [
            tag_name
            for tag_name in tags.keys()
            if tag_name not in self.exclude_tags_list
        ]
        mp4_tags = {}
        for tag_name in to_apply_tags:
            if tag_name in ("disc", "disc_total"):
                if mp4_tags.get("disk") is None:
                    mp4_tags["disk"] = [[0, 0]]
                if tag_name == "disc":
                    mp4_tags["disk"][0][0] = tags[tag_name]
                elif tag_name == "disc_total":
                    mp4_tags["disk"][0][1] = tags[tag_name]
            elif tag_name in ("track", "track_total"):
                if mp4_tags.get("trkn") is None:
                    mp4_tags["trkn"] = [[0, 0]]
                if tag_name == "track":
                    mp4_tags["trkn"][0][0] = tags[tag_name]
                elif tag_name == "track_total":
                    mp4_tags["trkn"][0][1] = tags[tag_name]
            elif tag_name == "compilation":
                mp4_tags["cpil"] = tags["compilation"]
            elif tag_name == "gapless":
                mp4_tags["pgap"] = tags["gapless"]
            elif (
                MP4_TAGS_MAP.get(tag_name) is not None
                and tags.get(tag_name) is not None
            ):
                mp4_tags[MP4_TAGS_MAP[tag_name]] = [tags[tag_name]]
        if (
            "cover" not in self.exclude_tags_list
            and self.cover_format != CoverFormat.RAW
        ):
            cover_bytes = self.get_cover_url_response_bytes(cover_url)
            if cover_bytes is not None:
                mp4_tags["covr"] = [
                    MP4Cover(
                        self.get_cover_url_response_bytes(cover_url),
                        imageformat=(
                            MP4Cover.FORMAT_JPEG
                            if self.cover_format == CoverFormat.JPG
                            else MP4Cover.FORMAT_PNG
                        ),
                    )
                ]
        mp4 = MP4(path)
        mp4.clear()
        mp4.update(mp4_tags)
        mp4.save()

    def move_to_output_path(
        self,
        remuxed_path: Path,
        final_path: Path,
    ):
        final_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(remuxed_path, final_path)

    @functools.lru_cache()
    def save_cover(self, cover_path: Path, cover_url: str):
        cover_path.parent.mkdir(parents=True, exist_ok=True)
        cover_path.write_bytes(self.get_cover_url_response_bytes(cover_url))

    def cleanup_temp_path(self):
        shutil.rmtree(self.temp_path)
