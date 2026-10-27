"""Lazy resolution of direct media, albums, playlists and Apple Music URLs."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from .base import Media, MediaSource
from .cover_cache import CoverCache
from .constants import ARTIST_COLLECTIONS, DEFAULT_AUTO_SELECT
from .enums import SelectionKind
from .exceptions import SelectionRequiredError, UnsupportedMediaError
from .models import (
    CollectionOption,
    CollectionSelectionRequest,
    SelectionOption,
    SelectionRequest,
)
from .playlist import Playlist
from .song import Song
from .music_video import MusicVideo
from .uploaded_video import UploadedVideo

if TYPE_CHECKING:
    from gamdl.api import AppleMusicApi


Selector = Callable[
    [SelectionRequest | CollectionSelectionRequest],
    Sequence[int] | Awaitable[Sequence[int]],
]


class MediaResolver:
    """Resolve source IDs/URLs to a stream of lazy media objects.

    Album and playlist tracks are selected automatically by default. Override
    ``auto_select`` to require an explicit ``selector`` for either source. A
    selector returns occurrence indices, preserving order and duplicate IDs.

    Artist selection can expand selected album categories, top songs, and music
    videos. Only explicitly enabled artist categories are processed without
    prompting; the default auto-selection applies to album/playlist tracks.
    """

    def __init__(
        self,
        api: AppleMusicApi,
        *,
        selector: Selector | None = None,
        auto_select: Iterable[SelectionKind | str] = DEFAULT_AUTO_SELECT,
        cover_cache: CoverCache | None = None,
    ) -> None:
        self.api = api
        self.selector = selector
        self.auto_select = frozenset(SelectionKind(item) for item in auto_select)
        self.cover_cache = cover_cache if cover_cache is not None else CoverCache()

    async def from_song(
        self, song_id: str, *, is_library: bool = False
    ) -> AsyncIterator[Media]:
        """Yield one unloaded song; no remote calls are required."""
        yield Song.from_id(
            song_id, api=self.api, is_library=is_library, cover_cache=self.cover_cache
        )

    async def from_music_video(
        self, media_id: str, *, is_library: bool = False
    ) -> AsyncIterator[Media]:
        """Yield one unloaded music video."""
        yield MusicVideo.from_id(
            media_id, api=self.api, is_library=is_library, cover_cache=self.cover_cache
        )

    async def from_uploaded_video(self, media_id: str) -> AsyncIterator[Media]:
        """Yield one unloaded uploaded video."""
        yield UploadedVideo.from_id(
            media_id, api=self.api, cover_cache=self.cover_cache
        )

    async def _from_collection(
        self,
        source_id: str,
        kind: SelectionKind,
        resources: AsyncIterator[dict],
        *,
        playlist: Playlist | None = None,
    ) -> AsyncIterator[Media]:
        """Stream auto-selected resources, materialize only for explicit UI selection."""
        if kind not in self.auto_select and self.selector is None:
            raise SelectionRequiredError(kind.value)

        def as_media(metadata: dict, index: int) -> Media:
            media = Media.from_metadata(
                metadata, api=self.api, cover_cache=self.cover_cache
            )
            media.source = MediaSource(
                kind=kind.value, id=source_id, index=index, playlist=playlist
            )
            return media

        if kind in self.auto_select:
            index = 0
            async for metadata in resources:
                yield as_media(metadata, index)
                index += 1
            return

        options: list[SelectionOption] = []
        async for metadata in resources:
            media = as_media(metadata, len(options))
            options.append(
                SelectionOption(
                    index=len(options),
                    title=metadata.get("attributes", {}).get("name", media.id),
                    media=media,
                )
            )
        assert self.selector is not None
        selected = self.selector(
            SelectionRequest(kind=kind, source_id=source_id, options=tuple(options))
        )
        indices = await self._selected_indices(selected, len(options))
        for option in options:
            if option.index in indices:
                yield option.media

    @staticmethod
    async def _selected_indices(selected, count: int) -> set[int]:
        if inspect.isawaitable(selected):
            selected = await selected
        if isinstance(selected, (str, bytes)) or not isinstance(selected, Sequence):
            raise ValueError("Selector must return a sequence of occurrence indices")
        if any(type(i) is not int or i < 0 or i >= count for i in selected):
            raise ValueError("Selector returned an invalid occurrence index")
        return set(selected)

    async def from_album(
        self, album_id: str, *, is_library: bool = False
    ) -> AsyncIterator[Media]:
        """Yield each album track in order with no album object or eager metadata."""
        resources = self.api.iter_album_tracks(album_id, is_library=is_library)
        async for media in self._from_collection(
            album_id, SelectionKind.ALBUM_TRACKS, resources
        ):
            yield media

    async def from_playlist(
        self, playlist_id: str, *, is_library: bool = False
    ) -> AsyncIterator[Media]:
        """Yield playlist tracks without deduplicating repeated entries."""
        resources = self.api.iter_playlist_tracks(playlist_id, is_library=is_library)
        playlist = Playlist(playlist_id, api=self.api, is_library=is_library)
        async for media in self._from_collection(
            playlist_id, SelectionKind.PLAYLIST_TRACKS, resources, playlist=playlist
        ):
            yield media

    async def from_artist(self, artist_id: str) -> AsyncIterator[Media]:
        """Expand artist categories and, where needed, individual albums.

        Automatic categories stream resources page by page. Interactive artist
        selection buffers only the category being presented as a checkbox.
        """
        categories = [kind for kind in ARTIST_COLLECTIONS if kind in self.auto_select]
        if not categories:
            if self.selector is None:
                raise SelectionRequiredError(SelectionKind.ARTIST.value)
            response = await self.api.get_artist(artist_id)
            entries = response.get("data", [])
            attributes = entries[0].get("attributes", {}) if entries else {}
            artist_name = attributes.get("name") or artist_id
            request = CollectionSelectionRequest(
                kind=SelectionKind.ARTIST,
                source_id=artist_id,
                prompt=f"Select content for {artist_name}:",
                options=tuple(
                    CollectionOption(i, entry[2])
                    for i, entry in enumerate(ARTIST_COLLECTIONS.values())
                ),
            )
            indices = await self._selected_indices(
                self.selector(request), len(request.options)
            )
            categories = [
                kind for i, kind in enumerate(ARTIST_COLLECTIONS) if i in indices
            ]

        for kind in categories:
            relation_type, path, title = ARTIST_COLLECTIONS[kind]
            resources = (
                self.api.iter_artist_view(artist_id, path)
                if relation_type == "views"
                else self.api.iter_artist_relationship(artist_id, path)
            )
            if kind not in self.auto_select:
                assert self.selector is not None
                items = [item async for item in resources]
                request = CollectionSelectionRequest(
                    kind=kind,
                    source_id=artist_id,
                    prompt=f"Select {title.lower()} to download:",
                    options=tuple(
                        CollectionOption(
                            i, item.get("attributes", {}).get("name") or item["id"]
                        )
                        for i, item in enumerate(items)
                    ),
                )
                selected = (
                    await self._selected_indices(self.selector(request), len(items))
                    if items
                    else set()
                )

                async def selected_items() -> AsyncIterator[tuple[int, dict]]:
                    for i, item in enumerate(items):
                        if i in selected:
                            yield i, item

                indexed_resources = selected_items()
            else:

                async def indexed_items() -> AsyncIterator[tuple[int, dict]]:
                    index = 0
                    async for item in resources:
                        yield index, item
                        index += 1

                indexed_resources = indexed_items()
            async for index, item in indexed_resources:
                media_type = item.get("type")
                if media_type == "albums":
                    async for media in self.from_album(item["id"]):
                        yield media
                elif media_type in {"songs", "music-videos"}:
                    media = Media.from_metadata(
                        item, api=self.api, cover_cache=self.cover_cache
                    )
                    media.source = MediaSource(kind.value, artist_id, index)
                    yield media

    async def from_library(self) -> AsyncIterator[Media]:
        """Reserve library selection for the next phase; never select blindly."""
        if SelectionKind.LIBRARY not in self.auto_select and self.selector is None:
            raise SelectionRequiredError(SelectionKind.LIBRARY.value)
        raise UnsupportedMediaError("Library selections are not implemented yet")
        yield

    async def from_url(self, url: str) -> AsyncIterator[Media]:
        """Resolve known Apple Music URLs without fetching their landing pages."""
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "music.apple.com"
            or parsed.port
        ):
            raise UnsupportedMediaError("Expected an HTTPS music.apple.com link")
        segments = [s for s in parsed.path.split("/") if s]
        if segments and len(segments[0]) == 2:
            segments.pop(0)  # storefront prefix, e.g. /br/album/...
        if not segments:
            raise UnsupportedMediaError("Apple Music URL has no media type")
        kind = segments[0]
        media_id = segments[-1]
        if kind == "library":
            async for media in self.from_library():
                yield media
            return
        if len(segments) < 2 or not media_id:
            raise UnsupportedMediaError("Apple Music URL has no media ID")
        if kind == "album":
            track_ids = parse_qs(parsed.query).get("i")
            if track_ids:
                async for media in self.from_song(track_ids[0]):
                    yield media
            else:
                async for media in self.from_album(media_id):
                    yield media
        elif kind == "playlist":
            async for media in self.from_playlist(media_id):
                yield media
        elif kind == "song":
            async for media in self.from_song(media_id):
                yield media
        elif kind == "music-video":
            async for media in self.from_music_video(media_id):
                yield media
        elif kind == "uploaded-video":
            async for media in self.from_uploaded_video(media_id):
                yield media
        elif kind == "artist":
            async for media in self.from_artist(media_id):
                yield media
        else:
            raise UnsupportedMediaError(f"Unsupported Apple Music URL type: {kind}")
