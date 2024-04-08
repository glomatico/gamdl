import re
import subprocess
import urllib.parse
from pathlib import Path

import click
import m3u8
from tabulate import tabulate

from .constants import MUSIC_VIDEO_CODEC_MAP
from .downloader import Downloader
from .enums import MusicVideoCodec, RemuxMode
from .models import StreamInfo


class DownloaderMusicVideo:
    MP4_FORMAT_CODECS = ["hvc1", "ec-3"]

    def __init__(
        self,
        downloader: Downloader,
        codec: MusicVideoCodec = MusicVideoCodec.H264_BEST,
    ):
        self.downloader = downloader
        self.codec = codec

    def get_stream_url_master(self, itunes_page: dict) -> str:
        return itunes_page["offers"][0]["assets"][0]["hlsUrl"]

    def get_m3u8_master_data(self, stream_url_master: str) -> dict:
        url_parts = urllib.parse.urlparse(stream_url_master)
        query = urllib.parse.parse_qs(url_parts.query, keep_blank_values=True)
        query.update({"aec": "HD", "dsid": "1"})
        stream_url_master_new = url_parts._replace(
            query=urllib.parse.urlencode(query, doseq=True)
        ).geturl()
        return m3u8.load(stream_url_master_new).data

    def get_playlist_video(
        self,
        playlists: list[dict],
    ) -> dict:
        playlists_filtered = [
            playlist
            for playlist in playlists
            if playlist["stream_info"]["codecs"].startswith(
                MUSIC_VIDEO_CODEC_MAP[self.codec]
            )
        ]
        if not playlists_filtered:
            playlists_filtered = [
                playlist
                for playlist in playlists
                if playlist["stream_info"]["codecs"].startswith(
                    MUSIC_VIDEO_CODEC_MAP[MusicVideoCodec.H264_BEST]
                )
            ]
        playlists_filtered.sort(key=lambda x: x["stream_info"]["bandwidth"])
        return playlists_filtered[-1]

    def get_playlist_video_from_user(
        self,
        playlists: list[dict],
    ) -> dict:
        table = [
            [
                i,
                playlist["stream_info"]["codecs"],
                playlist["stream_info"]["resolution"],
                playlist["stream_info"]["bandwidth"],
            ]
            for i, playlist in enumerate(playlists, 1)
        ]
        print(tabulate(table))
        try:
            choice = (
                click.prompt("Choose a video codec", type=click.IntRange(1, len(table)))
                - 1
            )
        except click.exceptions.Abort:
            raise KeyboardInterrupt()
        return playlists[choice]

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
        table = [
            [
                i,
                playlist["group_id"],
            ]
            for i, playlist in enumerate(playlists, 1)
        ]
        print(tabulate(table))
        try:
            choice = (
                click.prompt(
                    "Choose an audio codec", type=click.IntRange(1, len(table))
                )
                - 1
            )
        except click.exceptions.Abort:
            raise KeyboardInterrupt()
        return playlists[choice]

    def get_pssh(self, m3u8_data: dict):
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
        stream_info.pssh = self.get_pssh(m3u8_data)
        return stream_info

    def get_stream_info_audio(self, m3u8_master_data: dict) -> StreamInfo:
        stream_info = StreamInfo()
        if self.codec != MusicVideoCodec.ASK:
            playlist = self.get_playlist_audio(m3u8_master_data["media"])
        else:
            playlist = self.get_playlist_audio_from_user(m3u8_master_data["media"])
        stream_info.stream_url = playlist["uri"]
        stream_info.codec = re.search(r"_([^_]+)\.m3u8", stream_info.stream_url).group(
            1
        )
        m3u8_data = m3u8.load(stream_info.stream_url).data
        stream_info.pssh = self.get_pssh(m3u8_data)
        return stream_info

    def get_music_video_id_alt(self, metadata: dict) -> str:
        return metadata["attributes"]["url"].split("/")[-1].split("?")[0]

    def get_tags(
        self,
        itunes_page: dict,
        m3u8_master_data: dict,
        metadata: dict,
    ):
        tags = {
            "artist": metadata["attributes"]["artistName"],
            "artist_id": int(itunes_page["artistId"]),
            "copyright": itunes_page["copyright"],
            "date": next(
                (
                    session_data
                    for session_data in m3u8_master_data["session_data"]
                    if session_data["data_id"] == "com.apple.hls.release-date"
                ),
                None,
            )["value"],
            "genre": metadata["attributes"]["genreNames"][0],
            "genre_id": int(itunes_page["genres"][0]["genreId"]),
            "media_type": 6,
            "title": metadata["attributes"]["name"],
            "title_id": int(metadata["id"]),
        }
        if metadata["attributes"].get("contentRating") == "clean":
            tags["rating"] = 2
        elif metadata["attributes"].get("contentRating") == "explicit":
            tags["rating"] = 1
        else:
            tags["rating"] = 0
        if itunes_page.get("collectionId"):
            metadata_itunes = self.downloader.itunes_api.get_resource(itunes_page["id"])
            album = self.downloader.apple_music_api.get_album(
                itunes_page["collectionId"]
            )
            tags["album"] = album["attributes"]["name"]
            tags["album_artist"] = album["attributes"]["artistName"]
            tags["album_id"] = int(itunes_page["collectionId"])
            tags["disc"] = metadata_itunes[0]["discNumber"]
            tags["disc_total"] = metadata_itunes[0]["discCount"]
            tags["compilation"] = album["attributes"]["isCompilation"]
            tags["track"] = metadata_itunes[0]["trackNumber"]
            tags["track_total"] = metadata_itunes[0]["trackCount"]
        return tags

    def get_encrypted_path_video(self, track_id: str) -> str:
        return self.downloader.temp_path / f"encrypted_{track_id}.mp4"

    def get_encrypted_path_audio(self, track_id: str) -> str:
        return self.downloader.temp_path / f"encrypted_{track_id}.m4a"

    def get_decrypted_path_video(self, track_id: str) -> str:
        return self.downloader.temp_path / f"decrypted_{track_id}.mp4"

    def get_decrypted_path_audio(self, track_id: str) -> str:
        return self.downloader.temp_path / f"decrypted_{track_id}.m4a"

    def get_remuxed_path(self, track_id: str) -> str:
        return self.downloader.temp_path / f"remuxed_{track_id}.m4v"

    def decrypt(self, encrypted_path: Path, decryption_key: str, decrypted_path: Path):
        subprocess.run(
            [
                self.downloader.mp4decrypt_path_full,
                encrypted_path,
                "--key",
                f"1:{decryption_key}",
                decrypted_path,
            ],
            check=True,
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
                "-new",
                fixed_path,
            ],
            check=True,
        )

    def remux_ffmpeg(
        self,
        decrypted_path_video: Path,
        decrypte_path_audio: Path,
        fixed_path: Path,
        codec_video: str,
        codec_audio: str,
    ):
        use_mp4_flag = any(
            codec_video.startswith(codec) for codec in self.MP4_FORMAT_CODECS
        ) or any(codec_audio.startswith(codec) for codec in self.MP4_FORMAT_CODECS)
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
                "-f",
                "mp4" if use_mp4_flag else "ipod",
                "-c",
                "copy",
                "-c:s",
                "mov_text",
                fixed_path,
            ],
            check=True,
        )

    def remux(
        self,
        decrypted_path_video: Path,
        decrypted_path_audio: Path,
        remuxed_path: Path,
        codec_video: str,
        codec_audio: str,
    ):
        if self.downloader.remux_mode == RemuxMode.MP4BOX:
            self.remux_mp4box(
                decrypted_path_audio,
                decrypted_path_video,
                remuxed_path,
            )
        elif self.downloader.remux_mode == RemuxMode.FFMPEG:
            self.remux_ffmpeg(
                decrypted_path_video,
                decrypted_path_audio,
                remuxed_path,
                codec_video,
                codec_audio,
            )

    def get_cover_path(self, final_path: Path) -> Path:
        return final_path.with_suffix(f".{self.downloader.cover_format.value}")
