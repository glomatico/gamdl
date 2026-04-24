import asyncio
from typing import Any, AsyncGenerator, Callable

import structlog

from ..utils import safe_gather
from .constants import VALID_URL_PATTERN
from .enums import ArtistMediaType
from .exceptions import (
    GamdlInterfaceMediaNotAllowedError,
    GamdlInterfaceUrlParseError,
    GamdlInterfaceArtistMediaTypeError,
    GamdlInterfaceFlatFilterExcludedError,
)
from .music_video import AppleMusicMusicVideoInterface
from .song import AppleMusicSongInterface
from .types import AppleMusicMedia, AppleMusicUrlInfo
from .uploaded_video import AppleMusicUploadedVideoInterface

logger = structlog.get_logger(__name__)


class AppleMusicInterface:
    def __init__(
        self,
        song: AppleMusicSongInterface,
        music_video: AppleMusicMusicVideoInterface,
        uploaded_video: AppleMusicUploadedVideoInterface,
        artist_select_media_type_function: (
            Callable[[list[ArtistMediaType], dict], ArtistMediaType | None] | None
        ) = None,
        artist_select_items_function: (
            Callable[[ArtistMediaType, list[dict]], list[dict] | None] | None
        ) = None,
        flat_filter_function: Callable[[dict], Any] | None = None,
        concurrency: int = 1,
        disallowed_media_types: list[str] | None = None,
    ) -> None:
        self.song = song
        self.music_video = music_video
        self.uploaded_video = uploaded_video
        self.artist_select_media_type_function = artist_select_media_type_function
        self.artist_select_items_function = artist_select_items_function
        self.flat_filter_function = flat_filter_function
        self.concurrency = concurrency
        self.disallowed_media_types = disallowed_media_types

        self.base = song.base

    @staticmethod
    def get_url_info(url: str) -> AppleMusicUrlInfo | None:
        log = logger.bind(action="get_url_info", url=url)

        match = VALID_URL_PATTERN.match(url)
        if not match:
            log.debug("invalid_url_pattern")

            return None

        url_match = AppleMusicUrlInfo(
            **match.groupdict(),
        )

        log.debug("success", url_info=url_match)

        return url_match

    async def _run_flat_filter(self, media: AppleMusicMedia) -> None:
        if not self.flat_filter_function or not media.partial:
            return

        result = self.flat_filter_function(media.media_metadata)
        if asyncio.iscoroutine(result):
            result = await result

        if result:
            raise GamdlInterfaceFlatFilterExcludedError(media.media_id, result)

    def _run_media_type_filter(self, media: AppleMusicMedia) -> None:
        if not self.disallowed_media_types or not media.partial:
            return

        if media.media_metadata["type"] in self.disallowed_media_types:
            raise GamdlInterfaceMediaNotAllowedError(
                media.media_metadata["type"],
                media.media_id,
            )

    async def _get_song_media(
        self,
        media_id: str,
        index: int | None = None,
        total: int | None = None,
        media_metadata: dict | None = None,
        playlist_metadata: dict | None = None,
    ) -> AsyncGenerator[AppleMusicMedia, None]:
        media = AppleMusicMedia(
            media_id=media_id,
        )

        if index is not None:
            media.index = index
        if total is not None:
            media.total = total

        media.media_metadata = media_metadata
        media.playlist_metadata = playlist_metadata

        try:
            async for media in self.song.get_media(media):
                yield media

                self._run_media_type_filter(media)
                await self._run_flat_filter(media)
        except Exception as e:
            media.partial = False
            media.error = e
            yield media
            return

    async def _get_music_video_media(
        self,
        media_id: str,
        index: int | None = None,
        total: int | None = None,
        media_metadata: dict | None = None,
        playlist_metadata: dict | None = None,
    ) -> AsyncGenerator[AppleMusicMedia, None]:
        media = AppleMusicMedia(
            media_id=media_id,
        )

        if index is not None:
            media.index = index
        if total is not None:
            media.total = total

        media.media_metadata = media_metadata
        media.playlist_metadata = playlist_metadata

        try:
            async for media in self.music_video.get_media(media):
                yield media

                self._run_media_type_filter(media)
                await self._run_flat_filter(media)
        except Exception as e:
            media.partial = False
            media.error = e
            yield media
            return

    async def _get_uploaded_video_media(
        self,
        media_id: str,
    ) -> AsyncGenerator[AppleMusicMedia, None]:
        media = AppleMusicMedia(
            media_id=media_id,
        )

        try:
            async for media in self.music_video.get_media(media):
                yield

                self._run_media_type_filter(media)
                await self._run_flat_filter(media)
        except Exception as e:
            media.partial = False
            media.error = e
            yield media
            return

    async def _get_album_media(
        self,
        media_id: str,
        is_library: bool = False,
    ) -> AsyncGenerator[AppleMusicMedia, None]:
        base_media = AppleMusicMedia(media_id)

        try:
            base_media.media_metadata = (
                await self.base.apple_music_api.get_library_album(
                    media_id,
                )
                if is_library
                else await self.base.apple_music_api.get_album(
                    media_id,
                )
            )["data"][0]

            self._run_media_type_filter(base_media)
            await self._run_flat_filter(base_media)
        except Exception as e:
            base_media.partial = False
            base_media.error = e
            yield base_media
            return

        yield base_media

        tracks = base_media.media_metadata["relationships"]["tracks"]["data"]
        tasks = [
            (
                self._get_song_media(
                    media_id=track["id"],
                    index=index,
                    total=base_media.media_metadata["attributes"]["trackCount"],
                    media_metadata=track,
                )
                if track["type"] in {"songs", "library-songs"}
                else self._get_music_video_media(
                    media_id=track["id"],
                    index=index,
                    total=base_media.media_metadata["attributes"]["trackCount"],
                    media_metadata=track,
                )
            )
            for index, track in enumerate(tracks)
        ]

        for task in tasks:
            async for media in task:
                yield media

    async def _get_playlist_media(
        self,
        media_id: str,
        is_library: bool = False,
    ) -> AsyncGenerator[AppleMusicMedia, None]:
        base_media = AppleMusicMedia(media_id)

        try:
            base_media.media_metadata = (
                await self.base.apple_music_api.get_library_playlist(
                    media_id,
                )
                if is_library
                else await self.base.apple_music_api.get_playlist(
                    media_id,
                )
            )["data"][0]

            self._run_media_type_filter(base_media)
            await self._run_flat_filter(base_media)

            tracks = base_media.media_metadata["relationships"]["tracks"]["data"]
            next_uri = base_media.media_metadata["relationships"]["tracks"].get("next")
            href_uri = base_media.media_metadata["relationships"]["tracks"].get("href")
            while next_uri:
                extended_data = await self.base.apple_music_api.get_extended_api_data(
                    next_uri,
                    href_uri,
                )
                tracks.extend(extended_data["data"])
                next_uri = extended_data.get("next")
        except Exception as e:
            base_media.partial = False
            base_media.error = e
            yield base_media
            return

        yield base_media

        tasks = [
            (
                self._get_song_media(
                    media_id=track["id"],
                    index=index,
                    total=base_media.media_metadata["attributes"]["trackCount"],
                    media_metadata=track,
                    playlist_metadata=base_media.media_metadata,
                )
                if track["type"] in {"songs", "library-songs"}
                else self._get_music_video_media(
                    media_id=track["id"],
                    index=index,
                    total=base_media.media_metadata["attributes"]["trackCount"],
                    media_metadata=track,
                    playlist_metadata=base_media.media_metadata,
                )
            )
            for index, track in enumerate(tracks)
        ]

        for task in tasks:
            async for media in task:
                yield media

    async def _get_artist_media(
        self,
        media_id: str,
    ) -> AsyncGenerator[AppleMusicMedia, None]:
        base_media = AppleMusicMedia(media_id)

        try:
            base_media.media_metadata = (
                await self.base.apple_music_api.get_artist(
                    media_id,
                )
            )["data"][0]

            self._run_media_type_filter(base_media)
            await self._run_flat_filter(base_media)

            if self.artist_select_media_type_function:
                artist_media_type = self.artist_select_media_type_function(
                    list(ArtistMediaType),
                    base_media.media_metadata,
                )
                if asyncio.iscoroutine(artist_media_type):
                    artist_media_type = await artist_media_type
            else:
                artist_media_type = list(ArtistMediaType)[0]

            relation_key, type_key = artist_media_type.path_key

            items_relation = base_media.media_metadata.get(relation_key, {}).get(
                type_key, {}
            )
            items = items_relation.get("data", [])
            if not items:
                raise GamdlInterfaceArtistMediaTypeError(
                    base_media.media_id,
                    str(artist_media_type),
                )

            next_uri = items_relation.get("next")
            href_uri = items_relation.get("href")
            while next_uri:
                extended_data = await self.base.apple_music_api.get_extended_api_data(
                    next_uri,
                    href_uri,
                )
                items.extend(extended_data.get("data", []))
                next_uri = extended_data.get("next")
        except Exception as e:
            yield AppleMusicMedia(
                media_id=media_id,
                media_metadata=None,
                error=e,
            )
            return

        yield base_media

        if self.artist_select_items_function:
            selected_items = self.artist_select_items_function(
                artist_media_type,
                items,
            )
            if asyncio.iscoroutine(selected_items):
                selected_items = await selected_items
        else:
            selected_items = items[:1]

        tasks = []
        for index, item in enumerate(selected_items):
            if item["type"] in {"songs", "library-songs"}:
                tasks.append(
                    self._get_song_media(
                        media_id=item["id"],
                        index=index,
                        total=len(selected_items),
                        media_metadata=item,
                    )
                )
            elif item["type"] in {"albums", "library-albums"}:
                tasks.append(
                    self._get_album_media(
                        media_id=item["id"],
                    )
                )
            else:
                tasks.append(
                    self._get_music_video_media(
                        media_id=item["id"],
                        index=index,
                        total=len(selected_items),
                        media_metadata=item,
                    )
                )

        for task in tasks:
            async for media in task:
                yield media

    async def get_media_from_url(
        self,
        url: str,
    ) -> AsyncGenerator[AppleMusicMedia, None]:
        url_info = self.get_url_info(url)

        if not url_info:
            raise GamdlInterfaceUrlParseError(url)

        if self.disallowed_media_types and url_info.type in self.disallowed_media_types:
            raise GamdlInterfaceMediaNotAllowedError(
                url_info.type,
            )

        if url_info.type == "song" or url_info.sub_id:
            async for media in self._get_song_media(
                media_id=url_info.sub_id or url_info.id,
                index=0,
                total=1,
            ):
                yield media

        elif url_info.type == "music-video":
            async for media in self._get_music_video_media(
                media_id=url_info.id,
                index=0,
                total=1,
            ):
                yield media

        elif url_info.type == "album" or url_info.library_type == "albums":
            async for media in self._get_album_media(
                media_id=url_info.library_id or url_info.id,
                is_library=bool(url_info.library_type),
            ):
                yield media

        elif url_info.type == "playlist" or url_info.library_type == "playlist":
            async for media in self._get_playlist_media(
                media_id=url_info.library_id or url_info.id,
                is_library=bool(url_info.library_type),
            ):
                yield media

        elif url_info.type == "post":
            async for media in self._get_uploaded_video_media(
                media_id=url_info.id,
            ):
                yield media

        elif url_info.type == "artist":
            async for media in self._get_artist_media(
                media_id=url_info.id,
            ):
                yield media
