from pathlib import Path

from ..interface.enums import SongCodec, SyncedLyricsFormat
from ..interface.interface_song import AppleMusicSongInterface
from ..interface.types import DecryptionKeyAv
from ..utils import async_subprocess
from .amdecrypt import decrypt_file
from .constants import DEFAULT_SONG_DECRYPTION_KEY
from .downloader_base import AppleMusicBaseDownloader
from .enums import RemuxMode
from .types import DownloadItem


class AppleMusicSongDownloader(AppleMusicBaseDownloader):
    def __init__(
        self,
        base_downloader: AppleMusicBaseDownloader,
        interface: AppleMusicSongInterface,
        codec: SongCodec = SongCodec.AAC_LEGACY,
        synced_lyrics_format: SyncedLyricsFormat = SyncedLyricsFormat.LRC,
        no_synced_lyrics: bool = False,
        synced_lyrics_only: bool = False,
        use_album_date: bool = False,
        fetch_extra_tags: bool = False,
    ):
        self.__dict__.update(base_downloader.__dict__)
        self.interface = interface
        self.codec = codec
        self.synced_lyrics_format = synced_lyrics_format
        self.no_synced_lyrics = no_synced_lyrics
        self.synced_lyrics_only = synced_lyrics_only
        self.use_album_date = use_album_date
        self.fetch_extra_tags = fetch_extra_tags

    async def get_download_item(
        self,
        song_metadata: dict,
        playlist_metadata: dict = None,
    ) -> DownloadItem:
        download_item = DownloadItem()

        download_item.media_metadata = song_metadata
        download_item.playlist_metadata = playlist_metadata

        song_id = self.interface.get_media_id_of_library_media(song_metadata)

        download_item.lyrics = await self.interface.get_lyrics(
            song_metadata,
            self.synced_lyrics_format,
        )

        webplayback = await self.interface.apple_music_api.get_webplayback(song_id)
        download_item.media_tags = await self.interface.get_tags(
            webplayback,
            download_item.lyrics.unsynced if download_item.lyrics else None,
            self.use_album_date,
        )
        if self.fetch_extra_tags:
            download_item.extra_tags = await self.interface.get_extra_tags(
                song_metadata,
            )

        if playlist_metadata:
            download_item.playlist_tags = self.get_playlist_tags(
                playlist_metadata,
                song_metadata,
            )
            download_item.playlist_file_path = self.get_playlist_file_path(
                download_item.playlist_tags,
            )

        download_item.final_path = self.get_final_path(
            download_item.media_tags,
            ".m4a",
            download_item.playlist_tags,
        )
        download_item.synced_lyrics_path = self.get_lyrics_synced_path(
            download_item.final_path,
        )

        if self.synced_lyrics_only:
            return download_item

        if self.codec.is_legacy():
            download_item.stream_info = await self.interface.get_stream_info_legacy(
                webplayback,
                self.codec,
            )
            download_item.decryption_key = (
                await self.interface.get_decryption_key_legacy(
                    download_item.stream_info,
                    self.cdm,
                )
            )
        else:
            download_item.stream_info = await self.interface.get_stream_info(
                song_metadata,
                self.codec,
            )
            if (
                not self.use_wrapper
                and download_item.stream_info
                and download_item.stream_info.audio_track.widevine_pssh
            ):
                download_item.decryption_key = await self.interface.get_decryption_key(
                    download_item.stream_info,
                    self.cdm,
                )
            else:
                download_item.decryption_key = None

        download_item.cover_url_template = self.interface.get_cover_url_template(
            song_metadata,
            self.cover_format,
        )
        download_item.cover_url = self.interface.get_cover_url(
            download_item.cover_url_template,
            self.cover_size,
            self.cover_format,
        )

        download_item.random_uuid = self.get_random_uuid()
        if download_item.stream_info and download_item.stream_info.file_format:
            download_item.staged_path = self.get_temp_path(
                song_id,
                download_item.random_uuid,
                "staged",
                "." + download_item.stream_info.file_format.value,
            )
        else:
            download_item.staged_path = None

        cover_file_extension = await self.interface.get_cover_file_extension(
            download_item.cover_url,
            self.cover_format,
        )
        if cover_file_extension:
            download_item.cover_path = self.get_cover_path(
                download_item.final_path,
                cover_file_extension,
            )

        return download_item

    def fix_key_id(self, input_path: str):
        count = 0
        with open(input_path, "rb+") as file:
            while data := file.read(4096):
                pos = file.tell()
                i = 0
                while tenc := max(0, data.find(b"tenc", i)):
                    kid = tenc + 12
                    file.seek(max(0, pos - 4096) + kid, 0)
                    file.write(bytes.fromhex(f"{count:032}"))
                    count += 1
                    i = kid + 1
                file.seek(pos, 0)

    async def remux_mp4box(self, input_path: str, output_path: str):
        await async_subprocess(
            self.full_mp4box_path,
            "-quiet",
            "-add",
            input_path,
            "-itags",
            "artist=placeholder",
            "-keep-utc",
            "-new",
            output_path,
            silent=self.silent,
        )

    async def remux_ffmpeg(
        self,
        input_path: str,
        output_path: str,
        decryption_key: str = None,
    ):
        if decryption_key:
            key = [
                "-decryption_key",
                decryption_key,
            ]
        else:
            key = []

        await async_subprocess(
            self.full_ffmpeg_path,
            "-loglevel",
            "error",
            "-y",
            *key,
            "-i",
            input_path,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            output_path,
            silent=self.silent,
        )

    async def decrypt_mp4decrypt(
        self,
        input_path: str,
        output_path: str,
        decryption_key: str,
        legacy: bool,
    ):
        if legacy:
            keys = [
                "--key",
                f"1:{decryption_key}",
            ]
        else:
            self.fix_key_id(input_path)
            keys = [
                "--key",
                "0" * 31 + "1" + f":{decryption_key}",
                "--key",
                "0" * 32 + f":{DEFAULT_SONG_DECRYPTION_KEY}",
            ]

        await async_subprocess(
            self.full_mp4decrypt_path,
            *keys,
            input_path,
            output_path,
            silent=self.silent,
        )

    async def decrypt_amdecrypt(
        self,
        input_path: str,
        output_path: str,
        media_id: str,
        fairplay_key: str,
    ) -> None:
        await decrypt_file(
            self.wrapper_decrypt_ip,
            media_id,
            fairplay_key,
            input_path,
            output_path,
        )

    async def stage(
        self,
        encrypted_path: str,
        decrypted_path: str,
        staged_path: str,
        decryption_key: DecryptionKeyAv,
        codec: SongCodec,
        media_id: str,
        fairplay_key: str,
    ):
        if codec.is_legacy() and self.remux_mode == RemuxMode.FFMPEG:
            await self.remux_ffmpeg(
                encrypted_path,
                staged_path,
                decryption_key.audio_track.key,
            )
        elif codec.is_legacy() or not self.use_wrapper:
            await self.decrypt_mp4decrypt(
                encrypted_path,
                decrypted_path,
                decryption_key.audio_track.key,
                codec.is_legacy(),
            )
            if self.remux_mode == RemuxMode.FFMPEG:
                await self.remux_ffmpeg(
                    decrypted_path,
                    staged_path,
                )
            else:
                await self.remux_mp4box(
                    decrypted_path,
                    staged_path,
                )
        else:
            await self.decrypt_amdecrypt(
                encrypted_path,
                staged_path,
                media_id,
                fairplay_key,
            )

    def get_lyrics_synced_path(self, final_path: str) -> str:
        return str(Path(final_path).with_suffix("." + self.synced_lyrics_format.value))

    def get_cover_path(
        self,
        final_path: str,
        file_extension: str,
    ) -> str:
        return str(Path(final_path).parent / ("Cover" + file_extension))

    def write_synced_lyrics(
        self,
        synced_lyrics: str,
        lyrics_synced_path: str,
    ):
        Path(lyrics_synced_path).parent.mkdir(parents=True, exist_ok=True)
        Path(lyrics_synced_path).write_text(synced_lyrics, encoding="utf8")

    async def download(
        self,
        download_item: DownloadItem,
    ) -> None:
        if self.synced_lyrics_only:
            return

        encrypted_path = self.get_temp_path(
            download_item.media_metadata["id"],
            download_item.random_uuid,
            "encrypted",
            ".m4a",
        )
        await self.download_stream(
            download_item.stream_info.audio_track.stream_url,
            encrypted_path,
        )

        decrypted_path = self.get_temp_path(
            download_item.media_metadata["id"],
            download_item.random_uuid,
            "decrypted",
            ".m4a",
        )
        await self.stage(
            encrypted_path,
            decrypted_path,
            download_item.staged_path,
            download_item.decryption_key,
            self.codec,
            download_item.media_metadata["id"],
            download_item.stream_info.audio_track.fairplay_key,
        )

        cover_bytes = await self.interface.get_cover_bytes(download_item.cover_url)
        await self.apply_tags(
            download_item.staged_path,
            download_item.media_tags,
            cover_bytes,
            download_item.extra_tags,
        )
