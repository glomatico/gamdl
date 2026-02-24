import asyncio
import typing
from pathlib import Path

from InquirerPy import inquirer
from InquirerPy.base.control import Choice

from ..interface import AppleMusicInterface
from ..utils import safe_gather
from .constants import (
    ALBUM_MEDIA_TYPE,
    ARTIST_MEDIA_TYPE,
    MUSIC_VIDEO_MEDIA_TYPE,
    PLAYLIST_MEDIA_TYPE,
    SONG_MEDIA_TYPE,
    UPLOADED_VIDEO_MEDIA_TYPE,
    VALID_URL_PATTERN,
)
from .downloader_base import AppleMusicBaseDownloader
from .downloader_music_video import AppleMusicMusicVideoDownloader
from .downloader_song import AppleMusicSongDownloader
from .downloader_uploaded_video import AppleMusicUploadedVideoDownloader
from .enums import DownloadMode, RemuxMode
from .exceptions import (
    ExecutableNotFound,
    FormatNotAvailable,
    MediaFileExists,
    NotStreamable,
    SyncedLyricsOnly,
    UnsupportedMediaType,
)
from .types import DownloadItem, UrlInfo


class AppleMusicDownloader:
    def __init__(
        self,
        interface: AppleMusicInterface,
        base_downloader: AppleMusicBaseDownloader,
        song_downloader: AppleMusicSongDownloader,
        music_video_downloader: AppleMusicMusicVideoDownloader,
        uploaded_video_downloader: AppleMusicUploadedVideoDownloader,
        skip_music_videos: bool = False,
        skip_processing: bool = False,
        flat_filter: typing.Callable = None,
    ):
        self.interface = interface
        self.base_downloader = base_downloader
        self.song_downloader = song_downloader
        self.music_video_downloader = music_video_downloader
        self.uploaded_video_downloader = uploaded_video_downloader
        self.skip_music_videos = skip_music_videos
        self.skip_processing = skip_processing
        self.flat_filter = flat_filter

    async def get_single_download_item(
        self,
        media_metadata: dict,
        playlist_metadata: dict = None,
    ) -> DownloadItem:
        if self.flat_filter:
            flat_filter_result = self.flat_filter(media_metadata)
            if asyncio.iscoroutine(flat_filter_result):
                flat_filter_result = await flat_filter_result

            if flat_filter_result:
                return DownloadItem(
                    media_metadata=media_metadata,
                    playlist_metadata=playlist_metadata,
                    flat_filter_result=flat_filter_result,
                )

        return await self.get_single_download_item_no_filter(
            media_metadata,
            playlist_metadata,
        )

    async def get_single_download_item_no_filter(
        self,
        media_metadata: dict,
        playlist_metadata: dict = None,
    ) -> DownloadItem:
        try:
            if not self.base_downloader.is_media_streamable(
                media_metadata,
            ):
                raise NotStreamable(media_metadata["id"])

            if media_metadata["type"] in SONG_MEDIA_TYPE:
                if not self.song_downloader:
                    raise UnsupportedMediaType(media_metadata["type"])

                download_item = await self.song_downloader.get_download_item(
                    media_metadata,
                    playlist_metadata,
                )

            if media_metadata["type"] in MUSIC_VIDEO_MEDIA_TYPE:
                if not self.music_video_downloader:
                    raise UnsupportedMediaType(media_metadata["type"])

                download_item = await self.music_video_downloader.get_download_item(
                    media_metadata,
                    playlist_metadata,
                )

            if media_metadata["type"] in UPLOADED_VIDEO_MEDIA_TYPE:
                if not self.uploaded_video_downloader:
                    raise UnsupportedMediaType(media_metadata["type"])

                download_item = await self.uploaded_video_downloader.get_download_item(
                    media_metadata,
                )
        except Exception as e:
            download_item = DownloadItem(
                media_metadata=media_metadata,
                playlist_metadata=playlist_metadata,
                error=e,
            )

        return download_item

    async def get_collection_download_items(
        self,
        collection_metadata: dict,
    ) -> list[DownloadItem]:
        tracks_metadata = collection_metadata["relationships"]["tracks"]["data"]
        async for extended_data in self.interface.apple_music_api.extend_api_data(
            collection_metadata["relationships"]["tracks"],
        ):
            tracks_metadata.extend(extended_data["data"])

        tasks = [
            self.get_single_download_item(
                media_metadata,
                (
                    collection_metadata
                    if collection_metadata["type"] in PLAYLIST_MEDIA_TYPE
                    else None
                ),
            )
            for media_metadata in tracks_metadata
        ]

        download_items = await safe_gather(*tasks)
        return download_items

    async def get_artist_download_items(
        self,
        artist_metadata: dict,
    ) -> list[DownloadItem]:
        media_type = await inquirer.select(
            message=f'Select which type to download for artist "{artist_metadata["attributes"]["name"]}":',
            choices=[
                Choice(
                    name="Main Albums",
                    value=["views", "full-albums"],
                ),
                Choice(
                    name="Compilations Albums",
                    value=["views", "compilation-albums"],
                ),
                Choice(
                    name="Live Albums",
                    value=["views", "live-albums"],
                ),
                Choice(
                    name="Singles & EPs",
                    value=["views", "singles"],
                ),
                Choice(
                    name="All Albums",
                    value=["relationships", "albums"],
                ),
                Choice(
                    name="Top Songs",
                    value=["views", "top-songs"],
                ),
                Choice(
                    name="Music Videos",
                    value=["relationships", "music-videos"],
                ),
            ],
            validate=lambda result: artist_metadata.get(result[0], {})
            .get(result[1], {})
            .get("data"),
            invalid_message="The artist doesn't have any items of this type",
        ).execute_async()

        media_type, media_type_key = media_type
        artist_metadata[media_type][media_type_key]["data"].extend(
            [
                extended_data
                async for extended_data in self.interface.apple_music_api.extend_api_data(
                    artist_metadata[media_type][media_type_key],
                )
            ]
        )
        selected_tracks = artist_metadata[media_type][media_type_key]["data"]

        if media_type_key in {
            "full-albums",
            "compilation-albums",
            "live-albums",
            "singles",
            "albums",
        }:
            return await self.get_artist_albums_download_items(selected_tracks)
        elif media_type_key == "top-songs":
            return await self.get_artist_songs_download_items(selected_tracks)
        elif media_type_key == "music-videos":
            return await self.get_artist_music_videos_download_items(selected_tracks)

    async def get_artist_albums_download_items(
        self,
        albums_metadata: list[dict],
    ) -> list[DownloadItem]:
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
            for album in albums_metadata
            if album.get("attributes")
        ]
        selected = await inquirer.select(
            message="Select which albums to download: (Track Count | Release Date | Rating | Title)",
            choices=choices,
            multiselect=True,
        ).execute_async()

        download_items = []

        album_tasks = [
            self.interface.apple_music_api.get_album(album_metadata["id"])
            for album_metadata in selected
        ]
        album_responses = await safe_gather(*album_tasks)

        track_tasks = [
            self.get_collection_download_items(album_response["data"][0])
            for album_response in album_responses
        ]
        track_results = await safe_gather(*track_tasks)

        for track_result in track_results:
            download_items.extend(track_result)

        return download_items

    async def get_artist_music_videos_download_items(
        self,
        music_videos_metadata: list[dict],
    ) -> list[DownloadItem]:
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
            for music_video in music_videos_metadata
            if music_video.get("attributes")
        ]
        selected = await inquirer.select(
            message="Select which music videos to download: (Duration | Rating | Title)",
            choices=choices,
            multiselect=True,
        ).execute_async()

        music_video_tasks = [
            self.get_single_download_item(
                music_video_metadata,
            )
            for music_video_metadata in selected
        ]
        download_items = await safe_gather(*music_video_tasks)

        return download_items

    async def get_artist_songs_download_items(
        self,
        songs_metadata: list[dict],
    ) -> list[DownloadItem]:
        choices = [
            Choice(
                name=" | ".join(
                    [
                        self.millis_to_min_sec(song["attributes"]["durationInMillis"]),
                        f'{song["attributes"].get("contentRating", "None").title():<8}',
                        song["attributes"]["name"],
                    ],
                ),
                value=song,
            )
            for song in songs_metadata
            if song.get("attributes")
        ]
        selected = await inquirer.select(
            message="Select which songs to download: (Duration | Rating | Title)",
            choices=choices,
            multiselect=True,
        ).execute_async()

        song_tasks = [
            self.get_single_download_item(
                song_metadata,
            )
            for song_metadata in selected
        ]
        download_items = await safe_gather(*song_tasks)

        return download_items

    def millis_to_min_sec(self, millis) -> str:
        minutes, seconds = divmod(millis // 1000, 60)
        return f"{minutes:02}:{seconds:02}"

    def get_url_info(self, url: str) -> UrlInfo | None:
        match = VALID_URL_PATTERN.match(url)
        if not match:
            return None

        return UrlInfo(
            **match.groupdict(),
        )

    async def get_download_queue(
        self,
        url_info: UrlInfo,
    ) -> list[DownloadItem] | None:
        return await self._get_download_queue(
            "song" if url_info.sub_id else url_info.type or url_info.library_type,
            url_info.sub_id or url_info.id or url_info.library_id,
            url_info.library_id is not None,
        )

    async def _get_download_queue(
        self,
        url_type: str,
        id: str,
        is_library: bool,
    ) -> list[DownloadItem] | None:
        download_items = []

        if url_type in ARTIST_MEDIA_TYPE:
            artist_response = await self.interface.apple_music_api.get_artist(
                id,
            )

            if artist_response is None:
                return None

            download_items = await self.get_artist_download_items(
                artist_response["data"][0],
            )

        if url_type in SONG_MEDIA_TYPE:
            song_respose = await self.interface.apple_music_api.get_song(id)

            if song_respose is None:
                return None

            download_items.append(
                await self.get_single_download_item(song_respose["data"][0])
            )

        if url_type in ALBUM_MEDIA_TYPE:
            if is_library:
                album_response = await self.interface.apple_music_api.get_library_album(
                    id
                )
            else:
                album_response = await self.interface.apple_music_api.get_album(id)

            if album_response is None:
                return None

            download_items = await self.get_collection_download_items(
                album_response["data"][0],
            )

        if url_type in PLAYLIST_MEDIA_TYPE:
            if is_library:
                playlist_response = (
                    await self.interface.apple_music_api.get_library_playlist(id)
                )
            else:
                playlist_response = await self.interface.apple_music_api.get_playlist(
                    id
                )

            if playlist_response is None:
                return None

            download_items = await self.get_collection_download_items(
                playlist_response["data"][0],
            )

        if url_type in MUSIC_VIDEO_MEDIA_TYPE:
            music_video_response = await self.interface.apple_music_api.get_music_video(
                id
            )

            if music_video_response is None:
                return None

            download_items.append(
                await self.get_single_download_item(music_video_response["data"][0])
            )

        if url_type in UPLOADED_VIDEO_MEDIA_TYPE:
            uploaded_video = await self.interface.apple_music_api.get_uploaded_video(id)

            if uploaded_video is None:
                return None

            download_items.append(
                await self.get_single_download_item(uploaded_video["data"][0])
            )

        return download_items

    async def download(
        self,
        download_item: DownloadItem,
    ) -> DownloadItem:
        try:
            if download_item.flat_filter_result:
                download_item = await self.get_single_download_item_no_filter(
                    download_item.media_metadata,
                    download_item.playlist_metadata,
                )

            if download_item.error:
                raise download_item.error

            await self._initial_processing(download_item)
            await self._download(download_item)
            await self._final_processing(download_item)

            return download_item
        finally:
            if isinstance(download_item, DownloadItem) and not self.skip_processing:
                self.base_downloader.cleanup_temp(download_item.random_uuid)

    async def _download(
        self,
        download_item: DownloadItem,
    ) -> None:
        if (
            self.song_downloader.synced_lyrics_only
            and download_item.media_metadata["type"] not in SONG_MEDIA_TYPE
        ):
            raise SyncedLyricsOnly()

        if self.song_downloader.synced_lyrics_only:
            return

        if (
            Path(download_item.final_path).exists()
            and not self.base_downloader.overwrite
        ):
            raise MediaFileExists(download_item.final_path)

        if download_item.media_metadata["type"] in {
            *SONG_MEDIA_TYPE,
            *MUSIC_VIDEO_MEDIA_TYPE,
        }:
            if (
                not self.base_downloader.use_wrapper
                or download_item.media_metadata["type"] in MUSIC_VIDEO_MEDIA_TYPE
                or self.song_downloader.codec.is_legacy()
            ):
                if (
                    self.base_downloader.remux_mode == RemuxMode.FFMPEG
                    and not self.base_downloader.full_ffmpeg_path
                ):
                    raise ExecutableNotFound("ffmpeg")

                if (
                    self.base_downloader.remux_mode == RemuxMode.MP4BOX
                    and not self.base_downloader.full_mp4box_path
                ):
                    raise ExecutableNotFound("MP4Box")

                if (
                    download_item.media_metadata["type"] in MUSIC_VIDEO_MEDIA_TYPE
                    or self.base_downloader.remux_mode == RemuxMode.MP4BOX
                ) and not self.base_downloader.full_mp4decrypt_path:
                    raise ExecutableNotFound("mp4decrypt")

            if (
                self.base_downloader.download_mode == DownloadMode.NM3U8DLRE
                and not self.base_downloader.full_nm3u8dlre_path
            ):
                raise ExecutableNotFound("N_m3u8DL-RE")

            if (
                not download_item.stream_info
                or not download_item.stream_info.audio_track
                or not download_item.stream_info.audio_track.stream_url
                or (
                    (
                        not download_item.decryption_key
                        or not download_item.decryption_key.audio_track
                        or not download_item.decryption_key.audio_track.key
                    )
                    and not self.base_downloader.use_wrapper
                )
            ):
                raise FormatNotAvailable(download_item.media_metadata["id"])

        if download_item.media_metadata["type"] in SONG_MEDIA_TYPE:
            await self.song_downloader.download(download_item)

        if download_item.media_metadata["type"] in MUSIC_VIDEO_MEDIA_TYPE:
            await self.music_video_downloader.download(download_item)

        if download_item.media_metadata["type"] in UPLOADED_VIDEO_MEDIA_TYPE:
            await self.uploaded_video_downloader.download(download_item)

    async def _initial_processing(
        self,
        download_item: DownloadItem,
    ) -> None:
        if self.skip_processing:
            return

        if download_item.cover_path and self.base_downloader.save_cover:
            cover_bytes = await self.interface.get_cover_bytes(download_item.cover_url)
            if cover_bytes and (
                self.base_downloader.overwrite
                or not Path(download_item.cover_path).exists()
            ):
                self.base_downloader.write_cover_image(
                    cover_bytes,
                    download_item.cover_path,
                )

        if (
            download_item.lyrics
            and download_item.lyrics.synced
            and not self.song_downloader.no_synced_lyrics
            and (
                self.base_downloader.overwrite
                or not Path(download_item.synced_lyrics_path).exists()
            )
        ):
            self.song_downloader.write_synced_lyrics(
                download_item.lyrics.synced,
                download_item.synced_lyrics_path,
            )

        if download_item.playlist_tags and self.base_downloader.save_playlist:
            self.base_downloader.update_playlist_file(
                download_item.playlist_file_path,
                download_item.final_path,
                download_item.playlist_tags.playlist_track,
            )

    async def _final_processing(
        self,
        download_item: DownloadItem,
    ) -> None:
        if self.skip_processing:
            return

        if download_item.staged_path and Path(download_item.staged_path).exists():
            self.base_downloader.move_to_final_path(
                download_item.staged_path,
                download_item.final_path,
            )
