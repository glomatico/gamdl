from pathlib import Path

from ..interface.enums import CoverFormat
from ..interface.types import AppleMusicMedia, DecryptionKeyAv
from ..utils import async_subprocess
from .base import AppleMusicBaseDownloader
from .enums import RemuxFormatMusicVideo, RemuxMode
from .types import DownloadItem


class AppleMusicMusicVideoDownloader:
    def __init__(
        self,
        base: AppleMusicBaseDownloader,
        remux_mode: RemuxMode = RemuxMode.FFMPEG,
        remux_format: RemuxFormatMusicVideo = RemuxFormatMusicVideo.M4V,
    ):
        self.base = base
        self.remux_mode = remux_mode
        self.remux_format = remux_format

    async def _remux_mp4box(
        self,
        input_path_video: str,
        input_path_audio: str,
        output_path: str,
    ):
        await async_subprocess(
            self.base.full_mp4box_path,
            "-quiet",
            "-add",
            input_path_audio,
            "-add",
            input_path_video,
            "-itags",
            "artist=placeholder",
            "-keep-utc",
            "-new",
            output_path,
            silent=self.base.silent,
        )

    async def _remux_ffmpeg(
        self,
        input_path_video: str,
        input_path_audio: str,
        output_path: str,
    ):
        await async_subprocess(
            self.base.full_ffmpeg_path,
            "-loglevel",
            "error",
            "-y",
            "-i",
            input_path_video,
            "-i",
            input_path_audio,
            "-c",
            "copy",
            "-c:s",
            "mov_text",
            "-movflags",
            "+faststart",
            output_path,
            silent=self.base.silent,
        )

    async def _decrypt_mp4decrypt(
        self,
        input_path: str,
        output_path: str,
        decryption_key: str,
    ):
        await async_subprocess(
            self.base.full_mp4decrypt_path,
            "--key",
            f"1:{decryption_key}",
            input_path,
            output_path,
            silent=self.base.silent,
        )

    async def stage(
        self,
        encrypted_path_video: str,
        encrypted_path_audio: str,
        decrypted_path_video: str,
        decrypted_path_audio: str,
        staged_path: str,
        decryption_key: DecryptionKeyAv,
    ):
        await self._decrypt_mp4decrypt(
            encrypted_path_video,
            decrypted_path_video,
            decryption_key.video_track.key,
        )
        await self._decrypt_mp4decrypt(
            encrypted_path_audio,
            decrypted_path_audio,
            decryption_key.audio_track.key,
        )

        if self.remux_mode == RemuxMode.MP4BOX:
            await self._remux_mp4box(
                decrypted_path_video,
                decrypted_path_audio,
                staged_path,
            )
        else:
            await self._remux_ffmpeg(
                decrypted_path_video,
                decrypted_path_audio,
                staged_path,
            )

    def get_cover_path(
        self,
        final_path: str,
        file_extension: str,
    ) -> str:
        return str(Path(final_path).with_suffix(file_extension))

    async def get_download_item(
        self,
        media: AppleMusicMedia,
    ) -> DownloadItem:
        download_item = DownloadItem(media)

        download_item.staged_path = self.base.get_temp_path(
            media.media_metadata["id"],
            download_item.uuid_,
            "staged",
            "." + media.stream_info.file_format.value,
        )

        download_item.final_path = self.base.get_final_path(
            media.tags,
            "." + media.stream_info.file_format.value,
            media.playlist_tags,
        )

        if media.playlist_tags:
            download_item.playlist_file_path = self.base.get_playlist_file_path(
                media.playlist_tags,
            )

        download_item.cover_path = self.get_cover_path(
            download_item.final_path,
            media.cover.file_extension,
        )

        return download_item

    async def download(
        self,
        download_item: DownloadItem,
    ) -> None:
        encrypted_path_video = self.base.get_temp_path(
            download_item.media.media_metadata["id"],
            download_item.uuid_,
            "encrypted_video",
            ".mp4",
        )
        encrypted_path_audio = self.base.get_temp_path(
            download_item.media.media_metadata["id"],
            download_item.uuid_,
            "encrypted_audio",
            ".m4a",
        )

        await self.base.download_stream(
            download_item.media.stream_info.video_track.stream_url,
            encrypted_path_video,
        )
        await self.base.download_stream(
            download_item.media.stream_info.audio_track.stream_url,
            encrypted_path_audio,
        )

        decrypted_path_video = self.base.get_temp_path(
            download_item.media.media_metadata["id"],
            download_item.uuid_,
            "decrypted_video",
            ".mp4",
        )
        decrypted_path_audio = self.base.get_temp_path(
            download_item.media.media_metadata["id"],
            download_item.uuid_,
            "decrypted_audio",
            ".m4a",
        )

        await self.stage(
            encrypted_path_video,
            encrypted_path_audio,
            decrypted_path_video,
            decrypted_path_audio,
            download_item.staged_path,
            download_item.media.decryption_key,
        )

        cover_bytes = (
            await self.base.interface.base.get_cover_bytes(
                download_item.media.cover.url
            )
            if self.base.interface.base.cover_format != CoverFormat.RAW
            else None
        )
        await self.base.apply_tags(
            download_item.staged_path,
            download_item.media.tags,
            cover_bytes,
        )
