import re
from collections.abc import Callable
from pathlib import Path

from mutagen import MutagenError
from mutagen.mp4 import MP4

from ..interface.enums import CoverFormat
from ..interface.types import AppleMusicMedia, DecryptionKeyAv
from .ammuxer import decrypt_and_mux_hex
from .base import AppleMusicBaseDownloader
from .enums import RemuxFormatMusicVideo, RemuxMode
from .types import DownloadItem


class AppleMusicMusicVideoDownloader:
    def __init__(
        self,
        base: AppleMusicBaseDownloader,
        remux_format: RemuxFormatMusicVideo = RemuxFormatMusicVideo.M4V,
        get_registered_media_id_by_path: Callable[[str], str | None] | None = None,
    ):
        self.base = base
        self.remux_format = remux_format
        self.get_registered_media_id_by_path = get_registered_media_id_by_path

    @staticmethod
    def _get_media_id_from_path(path: Path) -> str | None:
        try:
            mp4 = MP4(path)
        except (MutagenError, OSError):
            return None

        if not mp4.tags:
            return None

        title_id = mp4.tags.get("cnID")
        if not title_id:
            return None

        if isinstance(title_id, (list, tuple)):
            title_id = title_id[0] if title_id else None

        return str(title_id) if title_id is not None else None

    def resolve_final_path(
        self,
        final_path: str,
        media_id: str | int,
    ) -> str:
        final_path_obj = Path(final_path)
        parent = final_path_obj.parent
        if not parent.exists():
            return final_path

        pattern = re.compile(
            rf"^{re.escape(final_path_obj.stem)}(?: (?P<index>[2-9][0-9]*))?"
            rf"{re.escape(final_path_obj.suffix)}$"
        )
        candidates: dict[int, Path] = {}

        for path in parent.iterdir():
            if not path.is_file():
                continue

            match = pattern.fullmatch(path.name)
            if not match:
                continue

            index = int(match.group("index") or 1)
            candidates[index] = path

        media_id = str(media_id)
        occupied_indexes = set(candidates)

        for index in sorted(candidates):
            path = candidates[index]
            registered_media_id = (
                self.get_registered_media_id_by_path(str(path))
                if self.get_registered_media_id_by_path
                else None
            )
            if registered_media_id is not None:
                if str(registered_media_id) == media_id:
                    return str(path)
                continue

            existing_media_id = self._get_media_id_from_path(path)
            if existing_media_id == media_id:
                return str(path)

        index = 1
        while index in occupied_indexes:
            index += 1

        if index == 1:
            return final_path

        return str(
            final_path_obj.with_name(
                f"{final_path_obj.stem} {index}{final_path_obj.suffix}"
            )
        )

    async def stage(
        self,
        encrypted_path_video: str,
        encrypted_path_audio: str,
        staged_path: str,
        decryption_key: DecryptionKeyAv,
        is_m4v: bool = False,
    ):
        await decrypt_and_mux_hex(
            decryption_key.audio_track.key,
            encrypted_path_audio,
            staged_path,
            decryption_key.video_track.key,
            encrypted_path_video,
            m4v_brand=is_m4v,
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
        download_item.final_path = self.resolve_final_path(
            download_item.final_path,
            media.tags.title_id,
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

        await self.stage(
            encrypted_path_video,
            encrypted_path_audio,
            download_item.staged_path,
            download_item.media.decryption_key,
            download_item.staged_path.endswith(".m4v"),
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
