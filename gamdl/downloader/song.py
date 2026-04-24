from pathlib import Path

import structlog

from ..interface.enums import CoverFormat
from ..interface.types import AppleMusicMedia, DecryptionKeyAv
from .amdecrypt import decrypt_file, decrypt_file_hex
from .base import AppleMusicBaseDownloader
from .types import DownloadItem

logger = structlog.get_logger(__name__)


class AppleMusicSongDownloader:
    def __init__(
        self,
        base: AppleMusicBaseDownloader,
    ):
        self.base = base

    async def get_download_item(self, media: AppleMusicMedia) -> DownloadItem:
        download_item = DownloadItem(media)

        if media.stream_info:
            download_item.staged_path = self.base.get_temp_path(
                media.media_metadata["id"],
                download_item.uuid_,
                "staged",
                "." + media.stream_info.file_format.value,
            )

        download_item.final_path = self.base.get_final_path(
            media.tags,
            ".m4a",
            media.playlist_tags,
        )

        if media.playlist_tags:
            download_item.playlist_file_path = self.base.get_playlist_file_path(
                media.playlist_tags,
            )

        download_item.synced_lyrics_path = self.get_synced_lyrics_path(
            download_item.final_path
        )

        download_item.cover_path = self.get_cover_path(
            download_item.final_path,
            media.cover.file_extension,
        )

        return download_item

    async def _decrypt_amdecrypt(
        self,
        input_path: str,
        output_path: str,
        media_id: str,
        fairplay_key: str,
    ) -> None:
        await decrypt_file(
            self.base.wrapper_decrypt_ip,
            media_id,
            fairplay_key,
            input_path,
            output_path,
        )

    async def _decrypt_amdecrypt_hex(
        self,
        input_path: str,
        output_path: str,
        decryption_key: str,
        legacy: bool = False,
    ) -> None:
        await decrypt_file_hex(
            input_path,
            output_path,
            decryption_key,
            legacy=legacy,
        )

    async def stage(
        self,
        encrypted_path: str,
        staged_path: str,
        decryption_key: DecryptionKeyAv,
        legacy: bool,
        media_id: str,
        fairplay_key: str,
    ):
        log = logger.bind(
            action="stage_song",
            media_id=media_id,
            encrypted_path=encrypted_path,
            staged_path=staged_path,
        )

        if self.base.interface.base.use_wrapper and not legacy:
            await self._decrypt_amdecrypt(
                encrypted_path,
                staged_path,
                media_id,
                fairplay_key,
            )
        else:
            await self._decrypt_amdecrypt_hex(
                encrypted_path,
                staged_path,
                decryption_key.audio_track.key,
                legacy,
            )

        log.debug("success")

    def get_synced_lyrics_path(self, final_path: str) -> str:
        log = logger.bind(action="get_synced_lyrics_path", final_path=final_path)

        synced_lyrics_path = str(
            Path(final_path).with_suffix(
                "." + self.base.interface.song.synced_lyrics_format.value
            )
        )

        log.debug("success", synced_lyrics_path=synced_lyrics_path)

        return synced_lyrics_path

    def get_cover_path(
        self,
        final_path: str,
        file_extension: str,
    ) -> str:
        log = logger.bind(
            action="get_song_cover_path",
            final_path=final_path,
            file_extension=file_extension,
        )

        cover_path = str(Path(final_path).parent / ("Cover" + file_extension))

        log.debug("success", cover_path=cover_path)

        return cover_path

    async def download(
        self,
        download_item: DownloadItem,
    ) -> None:
        encrypted_path = self.base.get_temp_path(
            download_item.media.media_metadata["id"],
            download_item.uuid_,
            "encrypted",
            ".m4a",
        )
        await self.base.download_stream(
            download_item.media.stream_info.audio_track.stream_url,
            encrypted_path,
        )

        await self.stage(
            encrypted_path,
            download_item.staged_path,
            download_item.media.decryption_key,
            download_item.media.stream_info.audio_track.legacy,
            download_item.media.media_metadata["id"],
            download_item.media.stream_info.audio_track.fairplay_key,
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
