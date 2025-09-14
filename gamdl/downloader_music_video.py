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
from .enums import (
    MediaFileFormat,
    MusicVideoCodec,
    MusicVideoResolution,
    RemuxFormatMusicVideo,
    RemuxMode,
)
from .exceptions import *
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
        codec: list[MusicVideoCodec] = [MusicVideoCodec.H264, MusicVideoCodec.H265],
        remux_format: RemuxFormatMusicVideo = RemuxFormatMusicVideo.M4V,
        resolution: MusicVideoResolution = MusicVideoResolution.R1080P,
    ) -> None:
        self.downloader = downloader
        self.codec = codec
        self.remux_format = remux_format
        self.resolution = resolution

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

    def get_video_playlist_from_resolution(
        self,
        playlists: list[m3u8.Playlist],
    ) -> m3u8.Playlist | None:
        playlists_filtered = set()
        for playlist in playlists:
            for codec in self.codec:
                if playlist.stream_info.codecs.startswith(codec.fourcc()):
                    playlists_filtered.add(playlist)

        if not playlists_filtered:
            return None

        playlists_filtered = list(playlists_filtered)

        def sort_key(playlist: m3u8.Playlist) -> tuple[int, int, int, int]:
            playlist_resolution = playlist.stream_info.resolution[-1]
            resolution_difference = abs(playlist_resolution - int(self.resolution))
            codec_preference = len(self.codec)
            for i, preferred_codec in enumerate(self.codec):
                if playlist.stream_info.codecs.startswith(preferred_codec.fourcc()):
                    codec_preference = i
                    break
            bandwidth = playlist.stream_info.bandwidth
            return (
                resolution_difference,
                codec_preference,
                -playlist_resolution,
                -bandwidth,
            )

        playlists_filtered.sort(key=sort_key)

        return playlists_filtered[0]

    def get_best_stereo_audio_playlist(
        self,
        playlist_master_data: dict,
    ) -> dict | None:
        audio_playlist = next(
            (
                media
                for media in playlist_master_data["media"]
                if media["group_id"] == "audio-stereo-256"
            ),
            None,
        )
        return audio_playlist

    def get_video_playlist_from_user(
        self,
        playlists: list[m3u8.Playlist],
    ) -> m3u8.Playlist:
        choices = [
            Choice(
                name=" | ".join(
                    [
                        playlist.stream_info.codecs[:4],
                        "x".join(str(v) for v in playlist.stream_info.resolution),
                        str(playlist.stream_info.bandwidth),
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

    def get_audio_playlist_from_user(
        self,
        playlist_master_data: dict,
    ) -> dict:
        choices = [
            Choice(
                name=playlist["group_id"],
                value=playlist,
            )
            for playlist in playlist_master_data["media"]
            if playlist.get("uri")
        ]
        selected = inquirer.select(
            message="Select which audio codec to download:",
            choices=choices,
        ).execute()

        return selected

    def get_pssh(self, m3u8_obj: m3u8.M3U8) -> str:
        return next(
            (
                key
                for key in m3u8_obj.keys
                if key.keyformat == "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
            ),
            None,
        ).uri

    def get_stream_info_video(
        self, playlist_master_m3u8_obj: m3u8.M3U8
    ) -> StreamInfo | None:
        stream_info = StreamInfo()

        if MusicVideoCodec.ASK not in self.codec:
            playlist = self.get_video_playlist_from_resolution(
                playlist_master_m3u8_obj.playlists
            )
        else:
            playlist = self.get_video_playlist_from_user(
                playlist_master_m3u8_obj.playlists
            )
        if not playlist:
            return None

        stream_info.stream_url = playlist.uri
        stream_info.codec = playlist.stream_info.codecs

        playlist_m3u8_obj = m3u8.load(stream_info.stream_url)
        stream_info.widevine_pssh = self.get_pssh(playlist_m3u8_obj)

        return stream_info

    def get_stream_info_audio(self, playlist_master_data: dict) -> StreamInfo | None:
        stream_info = StreamInfo()

        if self.codec != MusicVideoCodec.ASK:
            playlist = self.get_best_stereo_audio_playlist(playlist_master_data)
        else:
            playlist = self.get_audio_playlist_from_user(playlist_master_data)
        if not playlist:
            return None

        stream_info.stream_url = playlist["uri"]
        stream_info.codec = playlist["group_id"]

        playlist_m3u8_obj = m3u8.load(stream_info.stream_url)
        stream_info.widevine_pssh = self.get_pssh(playlist_m3u8_obj)

        return stream_info

    def _get_stream_info(
        self,
        stream_url: str,
    ) -> StreamInfoAv | None:
        playlist_master_m3u8_obj = m3u8.load(stream_url)

        stream_info_video = self.get_stream_info_video(playlist_master_m3u8_obj)
        stream_info_audio = self.get_stream_info_audio(playlist_master_m3u8_obj.data)
        if not stream_info_video or not stream_info_audio:
            return None

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
    ) -> StreamInfoAv | None:
        return self._get_stream_info(self.get_stream_url_from_webplayback(webplayback))

    def get_stream_info_from_itunes_page(
        self,
        itunes_page: dict,
    ) -> StreamInfoAv | None:
        return self._get_stream_info(self.get_stream_url_from_itunes_page(itunes_page))

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
            if not album:
                return tags

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

    import typing

    def download(
        self,
        media_id: str = None,
        media_metadata: dict = None,
        playlist_attributes: dict = None,
        playlist_track: int = None,
    ) -> typing.Generator[DownloadInfo, None, None]:
        yield from self.downloader._final_processing_wrapper(
            self._download,
            media_id,
            media_metadata,
            playlist_attributes,
            playlist_track,
        )

    def _download(
        self,
        media_id: str = None,
        media_metadata: dict = None,
        playlist_attributes: dict = None,
        playlist_track: int = None,
    ) -> typing.Generator[DownloadInfo, None, None]:
        download_info = DownloadInfo()
        yield download_info

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

        if media_metadata:
            media_id = self.downloader.get_media_id_of_library_media(media_metadata)
        download_info.media_id = media_id
        colored_media_id = color_text(media_id, colorama.Style.DIM)

        database_final_path = self.downloader.get_database_final_path(media_id)
        if database_final_path:
            download_info.final_path = database_final_path
            yield download_info
            raise MediaFileAlreadyExistsException(database_final_path)

        if not media_metadata:
            logger.debug(f"[{colored_media_id}] Getting Music Video metadata")
            media_metadata = self.downloader.apple_music_api.get_music_video(media_id)
        download_info.media_metadata = media_metadata

        if not self.downloader.is_media_streamable(media_metadata):
            yield download_info
            raise MediaNotStreamableException()

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

        if not stream_info:
            yield download_info
            raise MediaFormatNotAvailableException()

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
            yield download_info
            raise MediaFileAlreadyExistsException(final_path)

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
            f"[{colored_media_id}] "
            "Decrypting video/audio to "
            f'{decrypted_path_video}"/"{decrypted_path_audio}" '
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

        yield download_info
