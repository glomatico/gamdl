import asyncio
import re
import unicodedata
from pathlib import Path
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
        artist_views: str = "full-albums,compilation-albums,live-albums,singles,top-songs",
        artist_deduplicate_albums: bool = True,
        output_path: str | None = None,
    ) -> None:
        self.song = song
        self.music_video = music_video
        self.uploaded_video = uploaded_video
        self.artist_select_media_type_function = artist_select_media_type_function
        self.artist_select_items_function = artist_select_items_function
        self.flat_filter_function = flat_filter_function
        self.concurrency = concurrency
        self.disallowed_media_types = disallowed_media_types
        self.artist_views = artist_views
        self.artist_deduplicate_albums = artist_deduplicate_albums
        self.output_path = output_path

        self.base = song.base

    @staticmethod
    def _normalize_for_dedup(s: str) -> str:
        """Normalize for name-based dedup: fullwidth→ASCII, strip diacritics, lowercase."""
        fullwidth_map = str.maketrans({
            '？': '?', '！': '!', '：': ':', '；': ';',
            '＊': '*', '＂': '"', '＜': '<', '＞': '>',
            '｜': '|', '／': '/', '＼': '\\',
        })
        s = s.translate(fullwidth_map)
        decomposed = unicodedata.normalize('NFD', s)
        return ''.join(c for c in decomposed if unicodedata.category(c) != 'Mn').lower().strip()

    def _album_exists_on_disk(self, album_name: str, artist_name: str) -> bool:
        """Return True if a non-empty folder matching album_name already exists on disk.

        Searches under output_path/{initial}/{artist}/ and output_path/{artist}/ to
        cover both 3-level (initials/artist/album) and 2-level (artist/album) templates.
        Uses normalized substring matching so year prefixes like '(2004) ' and release
        suffixes like ' (ALBUM)' are ignored.
        """
        if not self.output_path:
            return False

        # Strip common release suffixes before comparing (e.g. "- Single", "- EP")
        _clean = re.sub(
            r'\s*-\s*(?:single|ep|single version|deluxe edition|deluxe version|'
            r'expanded edition|special edition|remastered|remaster)\s*$',
            '',
            album_name,
            flags=re.IGNORECASE,
        ).strip()
        norm_album = self._normalize_for_dedup(_clean or album_name)
        if not norm_album:
            return False

        output = Path(self.output_path)
        if not output.is_dir():
            return False

        norm_artist = self._normalize_for_dedup(artist_name)

        # First alpha char of artist name, ASCII-normalized → initials folder letter
        first = next((c for c in artist_name.upper() if c.isalpha()), '#')
        initials_char = ''.join(
            c for c in unicodedata.normalize('NFD', first)
            if unicodedata.category(c) != 'Mn'
        ) or '#'

        def has_matching_album(directory: Path) -> bool:
            try:
                for folder in directory.iterdir():
                    if not folder.is_dir():
                        continue
                    if norm_album in self._normalize_for_dedup(folder.name):
                        if any(folder.iterdir()):
                            return True
            except OSError:
                pass
            return False

        def artist_matches(name: str) -> bool:
            n = self._normalize_for_dedup(name)
            return bool(n) and (norm_artist in n or n in norm_artist)

        try:
            # 3-level: output/{initial}/{artist}/album_folder
            initials_dir = output / initials_char
            if initials_dir.is_dir():
                for d in initials_dir.iterdir():
                    if d.is_dir() and artist_matches(d.name) and has_matching_album(d):
                        return True

            # 2-level: output/{artist}/album_folder
            for d in output.iterdir():
                if d.is_dir() and artist_matches(d.name) and has_matching_album(d):
                    return True
        except OSError:
            pass

        return False

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

    async def _collect_generator(
        self, generator_or_coroutine: AsyncGenerator[AppleMusicMedia, None]
    ) -> list[AppleMusicMedia]:
        results = []
        async for result in generator_or_coroutine:
            results.append(result)
        return results

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

        if self.concurrency == 1:
            for task in tasks:
                async for media in task:
                    yield media
        else:
            collected_tasks = [self._collect_generator(task) for task in tasks]
            batches = await safe_gather(*collected_tasks, limit=self.concurrency)
            for batch in batches:
                for media in batch:
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
                    media_metadata=track,
                    playlist_metadata=base_media.media_metadata,
                )
                if track["type"] in {"songs", "library-songs"}
                else self._get_music_video_media(
                    media_id=track["id"],
                    index=index,
                    media_metadata=track,
                    playlist_metadata=base_media.media_metadata,
                )
            )
            for index, track in enumerate(tracks)
        ]

        if self.concurrency == 1:
            for task in tasks:
                async for media in task:
                    yield media
        else:
            collected_tasks = [self._collect_generator(task) for task in tasks]
            batches = await safe_gather(*collected_tasks, limit=self.concurrency)
            for batch in batches:
                for media in batch:
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
                    views=self.artist_views,
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

        # Deduplicate albums by normalized name to avoid downloading the same
        # compilation/album multiple times when Apple Music catalogues it under
        # different IDs or release years across storefronts.
        # Also checks disk so that albums already downloaded in a previous session
        # (possibly under a different year folder) are not re-downloaded.
        seen_album_names: set[str] = set()

        def _seen(item: dict) -> bool:
            if not self.artist_deduplicate_albums:
                return False
            if item.get("type") not in {"albums", "library-albums"}:
                return False
            attrs = item.get("attributes") or {}
            name = attrs.get("name", "")
            artist = attrs.get("artistName", "")
            key = name.strip().lower()
            if key in seen_album_names:
                return True
            # Cross-session disk check: skip if a matching folder already has files
            if self._album_exists_on_disk(name, artist):
                seen_album_names.add(key)
                return True
            seen_album_names.add(key)
            return False

        tasks = []
        for index, item in enumerate(selected_items):
            if _seen(item):
                continue
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

        if self.concurrency == 1:
            for task in tasks:
                async for media in task:
                    yield media
        else:
            collected_tasks = [self._collect_generator(task) for task in tasks]
            batches = await safe_gather(*collected_tasks, limit=self.concurrency)
            for batch in batches:
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
