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
            Callable[[list[ArtistMediaType]], ArtistMediaType] | None
        ) = None,
        artist_select_items_function: (
            Callable[[ArtistMediaType, list[dict]], list[dict]] | None
        ) = None,
        flat_filter_function: Callable[[dict], Any] | None = None,
        concurrency: int = 5,
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

        self._base = song._base

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

    async def _get_song_media(
        self,
        media_id: str | None = None,
        media_metadata: dict | None = None,
        playlist_metadata: dict | None = None,
        playlist_track: int | None = None,
    ) -> AppleMusicMedia:
        if not media_metadata:
            try:
                media_metadata = (
                    await self._base.apple_music_api.get_song(
                        media_id,
                    )
                )[
                    "data"
                ][0]
            except Exception as e:
                return AppleMusicMedia(
                    media_id=media_id,
                    media_metadata=None,
                    error=e,
                )

        if not media_id:
            media_id = self._base.parse_catalog_media_id(media_metadata)

        base_media = AppleMusicMedia(media_id, media_metadata)

        if self.flat_filter_function:
            flat_filter_result = self.flat_filter_function(media_metadata)

            if asyncio.iscoroutine(flat_filter_result):
                flat_filter_result = await flat_filter_result

            if flat_filter_result:
                base_media.flat_filter_result = flat_filter_result
                return base_media

        if (
            self.disallowed_media_types
            and base_media.media_metadata["type"] in self.disallowed_media_types
        ):
            base_media.error = GamdlInterfaceMediaNotAllowedError(
                base_media.media_metadata["type"],
                media_id,
            )
            return base_media

        try:
            return await self.song.get_media(
                media_metadata,
                playlist_metadata,
                playlist_track,
            )
        except Exception as e:
            base_media.error = e
            return base_media

    async def _get_music_video_media(
        self,
        media_id: str | None = None,
        media_metadata: dict | None = None,
        playlist_metadata: dict | None = None,
        playlist_track: int | None = None,
    ) -> AppleMusicMedia:
        if not media_metadata:
            try:
                media_metadata = (
                    await self._base.apple_music_api.get_music_video(
                        media_id,
                    )
                )["data"][0]
            except Exception as e:
                return AppleMusicMedia(
                    media_id=media_id,
                    media_metadata=None,
                    error=e,
                )

        if not media_id:
            media_id = self.music_video.parse_catalog_media_id(media_metadata)

        base_media = AppleMusicMedia(media_id, media_metadata)

        if self.flat_filter_function:
            flat_filter_result = self.flat_filter_function(media_metadata)

            if asyncio.iscoroutine(flat_filter_result):
                flat_filter_result = await flat_filter_result

            if flat_filter_result:
                base_media.flat_filter_result = flat_filter_result
                return base_media

        if (
            self.disallowed_media_types
            and base_media.media_metadata["type"] in self.disallowed_media_types
        ):
            base_media.error = GamdlInterfaceMediaNotAllowedError(
                base_media.media_metadata["type"],
                media_id,
            )
            return base_media

        try:
            return await self.music_video.get_media(
                media_metadata,
                playlist_metadata,
                playlist_track,
            )
        except Exception as e:
            base_media.error = e
            return base_media

    async def _get_uploaded_video_media(
        self,
        media_id: str,
    ) -> AppleMusicMedia:
        try:
            media_metadata = (
                await self._base.apple_music_api.get_uploaded_video(
                    media_id,
                )
            )["data"][0]
        except Exception as e:
            return AppleMusicMedia(
                media_id=media_id,
                media_metadata=None,
                error=e,
            )

        base_media = AppleMusicMedia(media_id, media_metadata)

        if self.flat_filter_function:
            flat_filter_result = self.flat_filter_function(media_metadata)

            if asyncio.iscoroutine(flat_filter_result):
                flat_filter_result = await flat_filter_result

            if flat_filter_result:
                base_media.flat_filter_result = flat_filter_result
                return base_media

        if (
            self.disallowed_media_types
            and base_media.media_metadata["type"] in self.disallowed_media_types
        ):
            base_media.error = GamdlInterfaceMediaNotAllowedError(
                base_media.media_metadata["type"],
                media_id,
            )
            return base_media

        try:
            return await self.uploaded_video.get_media(media_metadata)
        except Exception as e:
            base_media.error = e
            return base_media

    async def _get_album_media(
        self,
        media_id: str,
        is_library: bool = False,
    ) -> AsyncGenerator[AppleMusicMedia, None]:
        try:
            media_metadata = (
                await self._base.apple_music_api.get_library_album(
                    media_id,
                )
                if is_library
                else await self._base.apple_music_api.get_album(
                    media_id,
                )
            )["data"][0]
        except Exception as e:
            yield AppleMusicMedia(
                media_id=media_id,
                media_metadata=None,
                error=e,
            )
            return

        if self.flat_filter_function:
            flat_filter_result = self.flat_filter_function(media_metadata)

            if asyncio.iscoroutine(flat_filter_result):
                flat_filter_result = await flat_filter_result

            if flat_filter_result:
                yield AppleMusicMedia(
                    media_id=media_id,
                    media_metadata=media_metadata,
                    flat_filter_result=flat_filter_result,
                )
                return

        if (
            self.disallowed_media_types
            and media_metadata["type"] in self.disallowed_media_types
        ):
            yield AppleMusicMedia(
                media_id=media_id,
                media_metadata=media_metadata,
                error=GamdlInterfaceMediaNotAllowedError(
                    media_metadata["type"],
                    media_id,
                ),
            )
            return

        tracks = media_metadata["relationships"]["tracks"]["data"]
        tasks = [
            (
                self._get_song_media(
                    media_id=track["id"],
                    media_metadata=track,
                    playlist_metadata=media_metadata,
                )
                if track["type"] in {"songs", "library-songs"}
                else self._get_music_video_media(
                    media_id=track["id"],
                    media_metadata=track,
                    playlist_metadata=media_metadata,
                )
            )
            for track in tracks
        ]

        if self.concurrency == 1:
            for task in tasks:
                async for result in task:
                    yield result

        else:
            for task in await safe_gather(*tasks, limit=self.concurrency):
                yield task

    async def _get_playlist_media(
        self,
        media_id: str,
        is_library: bool = False,
    ) -> AsyncGenerator[AppleMusicMedia, None]:
        try:
            media_metadata = (
                await self._base.apple_music_api.get_library_playlist(
                    media_id,
                )
                if is_library
                else await self._base.apple_music_api.get_playlist(
                    media_id,
                )
            )["data"][0]
        except Exception as e:
            yield AppleMusicMedia(
                media_id=media_id,
                media_metadata=None,
                error=e,
            )
            return

        if self.flat_filter_function:
            flat_filter_result = self.flat_filter_function(media_metadata)

            if asyncio.iscoroutine(flat_filter_result):
                flat_filter_result = await flat_filter_result

            if flat_filter_result:
                yield AppleMusicMedia(
                    media_id=media_id,
                    media_metadata=media_metadata,
                    flat_filter_result=flat_filter_result,
                )
                return

        if (
            self.disallowed_media_types
            and media_metadata["type"] in self.disallowed_media_types
        ):
            yield AppleMusicMedia(
                media_id=media_id,
                media_metadata=media_metadata,
                error=GamdlInterfaceMediaNotAllowedError(
                    media_metadata["type"],
                    media_id,
                ),
            )
            return

        tracks = media_metadata["relationships"]["tracks"]["data"]
        next_uri = media_metadata["relationships"]["tracks"].get("next")
        href_uri = media_metadata["relationships"]["tracks"].get("href")
        while next_uri:
            try:
                extended_data = await self._base.apple_music_api.get_extended_api_data(
                    next_uri,
                    href_uri,
                )
            except Exception as e:
                yield AppleMusicMedia(
                    media_id=media_id,
                    media_metadata=media_metadata,
                    error=e,
                )
                return
            tracks.extend(extended_data["data"])
            next_uri = extended_data.get("next")

        tasks = [
            (
                self._get_song_media(
                    media_id=track["id"],
                    media_metadata=track,
                    playlist_metadata=media_metadata,
                    playlist_track=index + 1,
                )
                if track["type"] in {"songs", "library-songs"}
                else self._get_music_video_media(
                    media_id=track["id"],
                    media_metadata=track,
                    playlist_metadata=media_metadata,
                    playlist_track=index + 1,
                )
            )
            for index, track in enumerate(tracks)
        ]

        if self.concurrency == 1:
            for task in tasks:
                async for result in task:
                    yield result

        else:
            for task in await safe_gather(*tasks, limit=self.concurrency):
                yield task

    async def _get_artist_media(
        self,
        media_id: str,
    ) -> AsyncGenerator[AppleMusicMedia, None]:
        try:
            media_metadata = (
                await self._base.apple_music_api.get_artist(
                    media_id,
                )
            )[
                "data"
            ][0]
        except Exception as e:
            yield AppleMusicMedia(
                media_id=media_id,
                media_metadata=None,
                error=e,
            )
            return

        if self.flat_filter_function:
            flat_filter_result = self.flat_filter_function(media_metadata)

            if asyncio.iscoroutine(flat_filter_result):
                flat_filter_result = await flat_filter_result

            if flat_filter_result:
                yield AppleMusicMedia(
                    media_id=media_id,
                    media_metadata=media_metadata,
                    flat_filter_result=flat_filter_result,
                )
                return

        if (
            self.disallowed_media_types
            and media_metadata["type"] in self.disallowed_media_types
        ):
            yield AppleMusicMedia(
                media_id=media_id,
                media_metadata=media_metadata,
                error=GamdlInterfaceMediaNotAllowedError(
                    media_metadata["type"],
                    media_id,
                ),
            )
            return

        artist_media_type = (
            self.artist_select_media_type_function(list(ArtistMediaType))
            if self.artist_select_media_type_function
            else list(ArtistMediaType)[0]
        )
        relation_key, type_key = artist_media_type.path_key

        items_relation = media_metadata.get(relation_key, {}).get(type_key, {})
        items = items_relation.get("data", [])
        if not items:
            yield AppleMusicMedia(
                media_id=media_id,
                media_metadata=media_metadata,
                error=GamdlInterfaceArtistMediaTypeError(str(artist_media_type)),
            )
            return

        next_uri = items_relation.get("next")
        href_uri = items_relation.get("href")
        while next_uri:
            try:
                extended_data = await self._base.apple_music_api.get_extended_api_data(
                    next_uri,
                    href_uri,
                )
            except Exception as e:
                yield AppleMusicMedia(
                    media_id=media_id,
                    media_metadata=media_metadata,
                    error=e,
                )
                return
            items.extend(extended_data.get("data", []))
            next_uri = extended_data.get("next")

        selected_items = (
            self.artist_select_items_function(
                artist_media_type,
                items,
            )
            if self.artist_select_items_function
            else items[:1]
        )

        tasks = []
        for item in selected_items:
            if item["type"] in {"songs", "library-songs"}:
                tasks.append(
                    (
                        item["type"],
                        self._get_song_media(
                            media_id=item["id"],
                            media_metadata=item,
                        ),
                    )
                )
            elif item["type"] in {"albums", "library-albums"}:
                tasks.append(
                    (
                        item["type"],
                        self._get_album_media(
                            media_id=item["id"],
                        ),
                    )
                )
            else:
                tasks.append(
                    (
                        item["type"],
                        self._get_music_video_media(
                            media_id=item["id"],
                            media_metadata=item,
                        ),
                    )
                )

        if self.concurrency == 1:
            for item_type, task in tasks:
                if item_type in {"albums", "library-albums"}:
                    async for result in task:
                        yield result
                else:
                    yield await task

        else:

            async def _collect_generator(generator_or_coroutine, item_type):
                if item_type in {"albums", "library-albums"}:
                    results = []
                    async for result in generator_or_coroutine:
                        results.append(result)
                    return results
                else:
                    return [await generator_or_coroutine]

            collected_tasks = [
                _collect_generator(task, item_type) for item_type, task in tasks
            ]
            for batch in await safe_gather(*collected_tasks, limit=self.concurrency):
                for media in batch:
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
            media = await self._get_song_media(
                media_id=url_info.sub_id or url_info.id,
            )
            yield media

        elif url_info.type == "music-video":
            media = await self._get_music_video_media(
                media_id=url_info.id,
            )
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
            media = await self._get_uploaded_video_media(
                media_id=url_info.id,
            )
            yield media

        elif url_info.type == "artist":
            async for media in self._get_artist_media(
                media_id=url_info.id,
            ):
                yield media
