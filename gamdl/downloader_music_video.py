from __future__ import annotations

import logging
import subprocess
import urllib.parse
from pathlib import Path

import colorama
import m3u8
from InquirerPy import inquirer
from InquirerPy.base.control import Choice

from .downloader import Downloader
from .enums import MediaFileFormat, MusicVideoCodec, RemuxFormatMusicVideo, RemuxMode
from .models import (
    DecryptionKeyAv,
    DownloadInfo,
    MediaRating,
    MediaTags,
    MediaType,
    StreamInfo,
    StreamInfoAv,
)
from .utils import color_text

logger = logging.getLogger("gamdl")


class DownloaderMusicVideo:
    MP4_FORMAT_CODECS = ["hvc1", "audio-atmos", "audio-ec3"]

    def __init__(
        self,
        downloader: Downloader,
        codec: MusicVideoCodec = MusicVideoCodec.H264,
        remux_format: RemuxFormatMusicVideo = RemuxFormatMusicVideo.M4V,
    ) -> None:
        self.downloader = downloader
        self.codec = codec
        self.remux_format = remux_format

    def get_stream_url_from_webplayback(self, webplayback: dict) -> str:
        return webplayback["hls-playlist-url"]

    def get_stream_url_from_itunes_page(self, itunes_page: dict) -> dict:
        stream_url = itunes_page["offers"][0]["assets"][0]["hlsUrl"]
        url_parts = urllib.parse.urlparse(stream_url)
        query = urllib.parse.parse_qs(url_parts.query, keep_blank_values=True)
        query.update({"aec": "HD", "dsid": "1"})
        return url_parts._replace(
            query=urllib.parse.urlencode(query, doseq=True)
        ).geturl()

    def get_m3u8_master_data(self, stream_url_master: str) -> dict:
        return m3u8.load(stream_url_master).data

    def get_playlist_video(
        self,
        playlists: list[dict],
    ) -> dict:
        playlists_filtered = [
            playlist
            for playlist in playlists
            if playlist["stream_info"]["codecs"].startswith(self.codec.fourcc())
        ]
        if not playlists_filtered:
            playlists_filtered = [
                playlist
                for playlist in playlists
                if playlist["stream_info"]["codecs"].startswith(self.codec.fourcc())
            ]
        playlists_filtered.sort(key=lambda x: x["stream_info"]["bandwidth"])
        return playlists_filtered[-1]

    def get_playlist_video_from_user(
        self,
        playlists: list[dict],
    ) -> dict:
        choices = [
            Choice(
                name=" | ".join(
                    [
                        playlist["stream_info"]["codecs"][:4],
                        playlist["stream_info"]["resolution"],
                        str(playlist["stream_info"]["bandwidth"]),
                    ]
                ),
                value=playlist,
            )
            for playlist in playlists
        ]
        selected = inquirer.select(
            message="Select which video codec to download: (Codec | Resolution | Bitrate)",
            choices=choices,
        ).execute()
        return selected

    def get_playlist_audio(
        self,
        playlists: list[dict],
    ) -> dict:
        stream_url = next(
            (
                playlist
                for playlist in playlists
                if playlist["group_id"] == "audio-stereo-256"
            ),
            None,
        )
        return stream_url

    def get_playlist_audio_from_user(
        self,
        playlists: list[dict],
    ) -> dict:
        choices = [
            Choice(
                name=playlist["group_id"],
                value=playlist,
            )
            for playlist in playlists
            if playlist.get("uri")
        ]
        selected = inquirer.select(
            message="Select which audio codec to download:",
            choices=choices,
        ).execute()
        return selected

    def get_pssh(self, m3u8_data: dict) -> str:
        return next(
            (
                key
                for key in m3u8_data["keys"]
                if key["keyformat"] == "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
            ),
            None,
        )["uri"]

    def get_stream_info_video(self, m3u8_master_data: dict) -> StreamInfo:
        stream_info = StreamInfo()
        if self.codec != MusicVideoCodec.ASK:
            playlist = self.get_playlist_video(m3u8_master_data["playlists"])
        else:
            playlist = self.get_playlist_video_from_user(m3u8_master_data["playlists"])
        stream_info.stream_url = playlist["uri"]
        stream_info.codec = playlist["stream_info"]["codecs"]
        m3u8_data = m3u8.load(stream_info.stream_url).data
        stream_info.widevine_pssh = self.get_pssh(m3u8_data)
        return stream_info

    def get_stream_info_audio(self, m3u8_master_data: dict) -> StreamInfo:
        stream_info = StreamInfo()
        if self.codec != MusicVideoCodec.ASK:
            playlist = self.get_playlist_audio(m3u8_master_data["media"])
        else:
            playlist = self.get_playlist_audio_from_user(m3u8_master_data["media"])
        stream_info.stream_url = playlist["uri"]
        stream_info.codec = playlist["group_id"]
        m3u8_data = m3u8.load(stream_info.stream_url).data
        stream_info.widevine_pssh = self.get_pssh(m3u8_data)
        return stream_info

    def _get_stream_info(
        self,
        m3u8_master_data: dict,
    ) -> StreamInfoAv:
        stream_info_video = self.get_stream_info_video(m3u8_master_data)
        stream_info_audio = self.get_stream_info_audio(m3u8_master_data)
        use_mp4 = (
            any(
                stream_info_video.codec.startswith(codec)
                for codec in self.MP4_FORMAT_CODECS
            )
            or any(
                stream_info_audio.codec.startswith(codec)
                for codec in self.MP4_FORMAT_CODECS
            )
            or self.remux_format == RemuxFormatMusicVideo.MP4
        )
        if use_mp4:
            file_format = MediaFileFormat.MP4
        else:
            file_format = MediaFileFormat.M4V
        return StreamInfoAv(
            video_track=stream_info_video,
            audio_track=stream_info_audio,
            file_format=file_format,
        )

    def get_stream_info_from_webplayback(
        self,
        webplayback: dict,
    ) -> StreamInfoAv:
        m3u8_master_data = self.get_m3u8_master_data(
            self.get_stream_url_from_webplayback(webplayback)
        )
        return self._get_stream_info(m3u8_master_data)

    def get_stream_info_from_itunes_page(
        self,
        itunes_page: dict,
    ) -> StreamInfoAv:
        m3u8_master_data = self.get_m3u8_master_data(
            self.get_stream_url_from_itunes_page(itunes_page)
        )
        return self._get_stream_info(m3u8_master_data)

    def get_decryption_key(
        self,
        stream_info: StreamInfoAv,
        media_id: str,
    ) -> DecryptionKeyAv:
        decryption_key_video = self.downloader.get_decryption_key(
            stream_info.video_track.widevine_pssh,
            media_id,
        )
        decryption_key_audio = self.downloader.get_decryption_key(
            stream_info.audio_track.widevine_pssh,
            media_id,
        )

        return DecryptionKeyAv(
            video_track=decryption_key_video,
            audio_track=decryption_key_audio,
        )

    def get_music_video_id_alt(self, metadata: dict) -> str | None:
        music_video_url = metadata["attributes"].get("url")
        if music_video_url is None:
            return None
        return music_video_url.split("/")[-1].split("?")[0]

    def get_tags(
        self,
        id_alt: str,
        itunes_page: dict,
        metadata: dict,
    ) -> MediaTags:
        metadata_itunes = self.downloader.itunes_api.get_resource(id_alt)

        explicitness = metadata_itunes[0]["trackExplicitness"]
        if explicitness == "notExplicit":
            rating = MediaRating.NONE
        elif explicitness == "explicit":
            rating = MediaRating.EXPLICIT
        else:
            rating = MediaRating.CLEAN

        tags = MediaTags(
            artist=metadata_itunes[0]["artistName"],
            artist_id=int(metadata_itunes[0]["artistId"]),
            copyright=itunes_page.get("copyright"),
            date=self.downloader.parse_date(metadata_itunes[0]["releaseDate"]),
            genre=metadata_itunes[0]["primaryGenreName"],
            genre_id=int(itunes_page["genres"][0]["genreId"]),
            media_type=MediaType.MUSIC_VIDEO,
            storefront=int(self.downloader.itunes_api.storefront_id.split("-")[0]),
            title=metadata_itunes[0]["trackCensoredName"],
            title_id=int(metadata["id"]),
            rating=rating,
        )

        if len(metadata_itunes) > 1:
            album = self.downloader.apple_music_api.get_album(
                itunes_page["collectionId"]
            )
            tags.album = metadata_itunes[1]["collectionCensoredName"]
            tags.album_artist = metadata_itunes[1]["artistName"]
            tags.album_id = int(itunes_page["collectionId"])
            tags.disc = metadata_itunes[0]["discNumber"]
            tags.disc_total = metadata_itunes[0]["discCount"]
            tags.compilation = album["attributes"]["isCompilation"]
            tags.track = metadata_itunes[0]["trackNumber"]
            tags.track_total = metadata_itunes[0]["trackCount"]

        return tags

    def decrypt(
        self,
        encrypted_path: Path,
        decryption_key: str,
        decrypted_path: Path,
    ) -> None:
        subprocess.run(
            [
                self.downloader.mp4decrypt_path_full,
                encrypted_path,
                "--key",
                f"1:{decryption_key}",
                decrypted_path,
            ],
            check=True,
            **self.downloader.subprocess_additional_args,
        )

    def remux_mp4box(
        self,
        decrypted_path_audio: Path,
        decrypted_path_video: Path,
        fixed_path: Path,
    ) -> None:
        subprocess.run(
            [
                self.downloader.mp4box_path_full,
                "-quiet",
                "-add",
                decrypted_path_audio,
                "-add",
                decrypted_path_video,
                "-itags",
                "artist=placeholder",
                "-keep-utc",
                "-new",
                fixed_path,
            ],
            check=True,
            **self.downloader.subprocess_additional_args,
        )

    def remux_ffmpeg(
        self,
        decrypted_path_video: Path,
        decrypte_path_audio: Path,
        fixed_path: Path,
    ) -> None:
        subprocess.run(
            [
                self.downloader.ffmpeg_path_full,
                "-loglevel",
                "error",
                "-y",
                "-i",
                decrypted_path_video,
                "-i",
                decrypte_path_audio,
                "-movflags",
                "+faststart",
                "-c",
                "copy",
                "-c:s",
                "mov_text",
                fixed_path,
            ],
            check=True,
            **self.downloader.subprocess_additional_args,
        )

    def stage(
        self,
        encrypted_path_video: Path,
        encrypted_path_audio: Path,
        decrypted_path_video: Path,
        decrypted_path_audio: Path,
        staged_path: Path,
        decryption_key: DecryptionKeyAv,
    ) -> None:
        self.decrypt(
            encrypted_path_video,
            decryption_key.video_track.key,
            decrypted_path_video,
        )
        self.decrypt(
            encrypted_path_audio,
            decryption_key.audio_track.key,
            decrypted_path_audio,
        )

        if self.downloader.remux_mode == RemuxMode.MP4BOX:
            self.remux_mp4box(
                decrypted_path_audio,
                decrypted_path_video,
                staged_path,
            )
        elif self.downloader.remux_mode == RemuxMode.FFMPEG:
            self.remux_ffmpeg(
                decrypted_path_video,
                decrypted_path_audio,
                staged_path,
            )

    def get_cover_path(self, final_path: Path, cover_format: str) -> Path:
        return final_path.with_suffix(
            self.downloader.get_cover_file_extension(cover_format)
        )

    def download(
        self,
        media_id: str = None,
        media_metadata: dict = None,
        playlist_attributes: dict = None,
        playlist_track: int = None,
    ) -> DownloadInfo:
        try:
            download_info = self._download(
                media_id,
                media_metadata,
                playlist_attributes,
                playlist_track,
            )
            self.downloader._final_processing(download_info)
        finally:
            self.downloader.cleanup_temp_path()
        return download_info

    def _download(
        self,
        media_id: str = None,
        media_metadata: dict = None,
        playlist_attributes: dict = None,
        playlist_track: int = None,
    ) -> DownloadInfo:
        download_info = DownloadInfo()

        if playlist_track is None and playlist_attributes:
            raise ValueError(
                "playlist_track must be provided if playlist_attributes is provided"
            )
        if playlist_attributes:
            playlist_tags = self.downloader.get_playlist_tags(
                playlist_attributes,
                playlist_track,
            )
        else:
            playlist_tags = None
        download_info.playlist_tags = playlist_tags

        if not media_id and not media_metadata:
            raise ValueError("Either media_id or media_metadata must be provided")

        if not media_id:
            media_id = self.downloader.get_media_id_of_library_media(media_metadata)
        download_info.media_id = media_id
        colored_media_id = color_text(media_id, colorama.Style.DIM)

        if not media_metadata:
            logger.debug(f"[{colored_media_id}] Getting Music Video metadata")
            media_metadata = self.downloader.apple_music_api.get_music_video(media_id)
        download_info.media_metadata = media_metadata

        if not self.downloader.is_media_streamable(media_metadata):
            logger.warning(
                f"[{color_text(media_metadata['id'], colorama.Style.DIM)}] "
                "Music Video is not streamable or downloadable, skipping"
            )
            return download_info

        alt_media_id = self.get_music_video_id_alt(media_metadata) or media_id
        download_info.alt_media_id = alt_media_id

        logger.debug(f"[{colored_media_id}] Getting iTunes page")
        itunes_page = self.downloader.itunes_api.get_itunes_page(
            "music-video",
            alt_media_id,
        )

        logger.debug(f"[{colored_media_id}] Getting tags")
        tags = self.get_tags(
            alt_media_id,
            itunes_page,
            media_metadata,
        )
        download_info.tags = tags

        if alt_media_id == media_id:
            logger.debug(f"[{colored_media_id}] Getting stream info")
            stream_info = self.get_stream_info_from_itunes_page(itunes_page)
        else:
            logger.debug(f"[{colored_media_id}] Getting webplayback info")
            webplayback = self.downloader.apple_music_api.get_webplayback(media_id)
            logger.debug(f"[{colored_media_id}] Getting stream info")
            stream_info = self.get_stream_info_from_webplayback(webplayback)
        download_info.stream_info = stream_info

        final_path = self.downloader.get_final_path(
            tags,
            self.downloader.get_media_file_extension(stream_info.file_format),
            playlist_tags,
        )
        download_info.final_path = final_path

        cover_url = self.downloader.get_cover_url(media_metadata)
        cover_format = self.downloader.get_cover_format(cover_url)
        if cover_format and self.downloader.save_cover:
            cover_path = self.get_cover_path(final_path, cover_format)
        else:
            cover_path = None
        download_info.cover_url = cover_url
        download_info.cover_format = cover_format
        download_info.cover_path = cover_path

        if final_path.exists() and not self.downloader.overwrite:
            logger.warning(
                f'[{colored_media_id}] Music Video already exists at "{final_path}", skipping'
            )
            return download_info

        logger.debug(f"[{colored_media_id}] Getting decryption key")
        decryption_key = self.get_decryption_key(
            stream_info,
            media_id,
        )

        encrypted_path_video = self.downloader.get_temp_path(
            media_id,
            "encrypted_video",
            ".mp4",
        )
        encrypted_path_audio = self.downloader.get_temp_path(
            media_id,
            "encrypted_audio",
            ".m4a",
        )
        decrypted_path_video = self.downloader.get_temp_path(
            media_id,
            "decrypted_video",
            ".mp4",
        )
        decrypted_path_audio = self.downloader.get_temp_path(
            media_id,
            "decrypted_audio",
            ".m4a",
        )
        staged_path = self.downloader.get_temp_path(
            media_id,
            "staged",
            self.downloader.get_media_file_extension(stream_info.file_format),
        )

        logger.info(f"[{colored_media_id}] Downloading Music Video")

        logger.debug(
            f'[{colored_media_id}] Downloading video to "{encrypted_path_video}"'
        )
        self.downloader.download(
            encrypted_path_video,
            stream_info.video_track.stream_url,
        )

        logger.debug(
            f'[{colored_media_id}] Downloading audio to "{encrypted_path_audio}"'
        )
        self.downloader.download(
            encrypted_path_audio,
            stream_info.audio_track.stream_url,
        )

        logger.debug(
            f'Decrypting video/audio to "{decrypted_path_video}"/"{decrypted_path_audio}" '
            f'and remuxing to "{staged_path}"'
        )
        self.stage(
            encrypted_path_video,
            encrypted_path_audio,
            decrypted_path_video,
            decrypted_path_audio,
            staged_path,
            decryption_key,
        )
        download_info.staged_path = staged_path

        return download_info
