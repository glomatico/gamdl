from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import m3u8
from pywidevine import PSSH
from pywidevine.license_protocol_pb2 import WidevinePsshData

from .downloader_song import DownloaderSong
from .enums import MediaFileFormat, RemuxMode, SongCodec
from .models import StreamInfo, StreamInfoAv


class DownloaderSongLegacy(DownloaderSong):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def get_stream_info(self, webplayback: dict) -> StreamInfoAv:
        flavor = "32:ctrp64" if self.codec == SongCodec.AAC_HE_LEGACY else "28:ctrp256"
        stream_info = StreamInfo()
        stream_info.stream_url = next(
            i for i in webplayback["assets"] if i["flavor"] == flavor
        )["URL"]
        m3u8_obj = m3u8.load(stream_info.stream_url)
        stream_info.widevine_pssh = m3u8_obj.keys[0].uri
        return StreamInfoAv(
            audio_track=stream_info,
            file_format=MediaFileFormat.M4A,
        )

    def get_decryption_key(self, pssh: str, track_id: str) -> str:
        try:
            widevine_pssh_data = WidevinePsshData()
            widevine_pssh_data.algorithm = 1
            widevine_pssh_data.key_ids.append(base64.b64decode(pssh.split(",")[1]))
            pssh_obj = PSSH(widevine_pssh_data.SerializeToString())
            cdm_session = self.downloader.cdm.open()
            challenge = base64.b64encode(
                self.downloader.cdm.get_license_challenge(cdm_session, pssh_obj)
            ).decode()
            license = self.downloader.apple_music_api.get_widevine_license(
                track_id,
                pssh,
                challenge,
            )
            self.downloader.cdm.parse_license(cdm_session, license)
            decryption_key = next(
                i
                for i in self.downloader.cdm.get_keys(cdm_session)
                if i.type == "CONTENT"
            ).key.hex()
        finally:
            self.downloader.cdm.close(cdm_session)
        return decryption_key

    def decrypt(
        self,
        encrypted_path: Path,
        decrypted_path: Path,
        decryption_key: str,
    ):
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

    def remux_mp4box(self, decrypted_path: Path, remuxed_path: Path):
        subprocess.run(
            [
                self.downloader.mp4box_path_full,
                "-quiet",
                "-add",
                decrypted_path,
                "-itags",
                "artist=placeholder",
                "-keep-utc",
                "-new",
                remuxed_path,
            ],
            check=True,
            **self.downloader.subprocess_additional_args,
        )

    def remux_ffmpeg(
        self,
        decryption_key: str,
        encrypted_path: Path,
        remuxed_path: Path,
    ):
        subprocess.run(
            [
                self.downloader.ffmpeg_path_full,
                "-loglevel",
                "error",
                "-y",
                "-decryption_key",
                decryption_key,
                "-i",
                encrypted_path,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                remuxed_path,
            ],
            check=True,
            **self.downloader.subprocess_additional_args,
        )

    def remux(
        self,
        encrypted_path: Path,
        decrypted_path: Path,
        remuxed_path: Path,
        decryption_key: str,
    ):
        if self.downloader.remux_mode == RemuxMode.FFMPEG:
            self.remux_ffmpeg(decryption_key, encrypted_path, remuxed_path)
        elif self.downloader.remux_mode == RemuxMode.MP4BOX:
            self.decrypt(encrypted_path, decrypted_path, decryption_key)
            self.remux_mp4box(decrypted_path, remuxed_path)
