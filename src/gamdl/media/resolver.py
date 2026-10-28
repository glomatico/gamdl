"""Lazy resolution of direct media, albums, playlists and Apple Music URLs."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from .base import Media, MediaSource
from .cover_cache import CoverCache
from .constants import ARTIST_COLLECTIONS, DEFAULT_AUTO_SELECT, LIBRARY_COLLECTIONS
from .enums import SelectionKind
from .exceptions import SelectionRequiredError, UnsupportedMediaError
from .models import (
    ArtistBrowseRequest,
    ArtistBrowseResult,
    LibraryBrowseRequest,
    CollectionOption,
    CollectionSelectionRequest,
    SelectionOption,
    SelectionRequest,
)
from .playlist import Playlist
from gamdl.pagination import MediaPage, PageCache
from .song import Song
from .music_video import MusicVideo
from .uploaded_video import UploadedVideo

if TYPE_CHECKING:
    from gamdl.api import AppleMusicApi


Selector = Callable[
    [
        SelectionRequest
        | CollectionSelectionRequest
        | ArtistBrowseRequest
        | LibraryBrowseRequest
    ],
    Sequence[int] | ArtistBrowseResult | Awaitable[Sequence[int] | ArtistBrowseResult],
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
        select_all: bool = False,
    ) -> AsyncIterator[Media]:
        """Stream auto-selected resources, materialize for explicit UI selection."""
        if not select_all and kind not in self.auto_select and self.selector is None:
            raise SelectionRequiredError(kind.value)

        def as_media(metadata: dict, index: int) -> Media:
            media = Media.from_metadata(
                metadata, api=self.api, cover_cache=self.cover_cache
            )
            media.source = MediaSource(
                kind=kind.value, id=source_id, index=index, playlist=playlist
            )
            return media

        if select_all or kind in self.auto_select:
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
        self, album_id: str, *, is_library: bool = False, select_all: bool = False
    ) -> AsyncIterator[Media]:
        """Yield each album track in order with no album object or eager metadata."""
        resources = self.api.iter_album_tracks(album_id, is_library=is_library)
        async for media in self._from_collection(
            album_id, SelectionKind.ALBUM_TRACKS, resources, select_all=select_all
        ):
            yield media

    async def from_playlist(
        self, playlist_id: str, *, is_library: bool = False, select_all: bool = False
    ) -> AsyncIterator[Media]:
        """Yield playlist tracks without deduplicating repeated entries."""
        resources = self.api.iter_playlist_tracks(playlist_id, is_library=is_library)
        playlist = Playlist(playlist_id, api=self.api, is_library=is_library)
        async for media in self._from_collection(
            playlist_id,
            SelectionKind.PLAYLIST_TRACKS,
            resources,
            playlist=playlist,
            select_all=select_all,
        ):
            yield media

    async def from_artist(self, artist_id: str) -> AsyncIterator[Media]:
        """Expand artist categories and, where needed, individual albums.

        Automatic categories stream resources page by page. The browser fetches
        a category only on expansion; selected unloaded categories stream later.
        """
        categories = [kind for kind in ARTIST_COLLECTIONS if kind in self.auto_select]
        cache: dict[SelectionKind, PageCache[dict]] = {}
        selected_items: dict[SelectionKind, set[int] | None] = {}
        excluded_items: dict[SelectionKind, set[int]] = {}
        if not categories:
            if self.selector is None:
                raise SelectionRequiredError(SelectionKind.ARTIST.value)
            # Only fetch the artist's name. No album or video view should be
            # populated before the user opens that category in the browser.
            response = await self.api.get_artist(
                artist_id, include="", views="", limit=1
            )
            entries = response.get("data", [])
            attributes = entries[0].get("attributes", {}) if entries else {}
            artist_name = attributes.get("name") or artist_id

            async def load_category_page(
                kind: SelectionKind,
                page_number: int,
            ) -> MediaPage[CollectionOption] | None:
                if kind not in ARTIST_COLLECTIONS:
                    raise ValueError("Unknown artist category")
                if kind not in cache:
                    relation, path, _ = ARTIST_COLLECTIONS[kind]

                    async def fetch(cursor: str | None) -> MediaPage[dict]:
                        if relation == "views":
                            return await self.api.get_artist_view_page(
                                artist_id,
                                path,
                                cursor=cursor,
                            )
                        return await self.api.get_artist_relationship_page(
                            artist_id,
                            path,
                            cursor=cursor,
                        )

                    cache[kind] = PageCache(fetch)
                collection = cache[kind]
                page = await collection.get(page_number)
                if page is None:
                    return None
                offset = collection.offset(page_number)
                return MediaPage(
                    tuple(
                        CollectionOption(
                            offset + i,
                            item.get("attributes", {}).get("name") or item["id"],
                        )
                        for i, item in enumerate(page.items)
                    ),
                    page.next_cursor,
                )

            request = ArtistBrowseRequest(
                source_id=artist_id,
                artist_name=artist_name,
                categories=tuple(
                    (kind, info[2]) for kind, info in ARTIST_COLLECTIONS.items()
                ),
                load_page=load_category_page,
            )
            result = self.selector(request)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, ArtistBrowseResult):
                raise ValueError("Artist browser must return ArtistBrowseResult")
            for selected in result.selections:
                if (
                    selected.kind not in ARTIST_COLLECTIONS
                    or selected.kind in selected_items
                ):
                    raise ValueError("Artist browser returned an invalid category")
                if selected.indices is not None:
                    items = cache.get(selected.kind)
                    if items is None:
                        raise ValueError(
                            "Artist item selection requires category metadata"
                        )
                    valid = set(range(sum(len(page.items) for page in items.pages)))
                    if any(
                        type(i) is not int or i not in valid for i in selected.indices
                    ):
                        raise ValueError(
                            "Artist browser returned an invalid item index"
                        )
                if selected.excluded_indices:
                    pages = cache.get(selected.kind)
                    valid = (
                        set(range(sum(len(page.items) for page in pages.pages)))
                        if pages
                        else set()
                    )
                    if selected.indices is not None or any(
                        type(i) is not int or i not in valid
                        for i in selected.excluded_indices
                    ):
                        raise ValueError("Invalid artist selection exclusions")
                selected_items[selected.kind] = (
                    set(selected.indices) if selected.indices is not None else None
                )
                excluded_items[selected.kind] = set(selected.excluded_indices)
            categories = [kind for kind in ARTIST_COLLECTIONS if kind in selected_items]

        for kind in categories:
            relation_type, path, _title = ARTIST_COLLECTIONS[kind]

            async def indexed_resources(
                kind=kind, path=path, relation_type=relation_type
            ) -> AsyncIterator[tuple[int, dict]]:
                if kind in cache:
                    if selected_items.get(kind) is not None:
                        # Explicit item selections are entirely inside the
                        # pages already visited. Do not fetch unseen pages
                        # just to discard their media.
                        collection = cache[kind]
                        for page_no, page in enumerate(collection.pages):
                            start = collection.offset(page_no)
                            for i, item in enumerate(page.items):
                                yield start + i, item
                    else:
                        async for i, item in cache[kind].iter_items():
                            yield i, item
                else:
                    resources = (
                        self.api.iter_artist_view(artist_id, path)
                        if relation_type == "views"
                        else self.api.iter_artist_relationship(artist_id, path)
                    )
                    i = 0
                    async for item in resources:
                        yield i, item
                        i += 1

            async for index, item in indexed_resources():
                if (
                    kind in selected_items
                    and selected_items[kind] is not None
                    and index not in selected_items[kind]
                ):
                    continue
                if index in excluded_items.get(kind, ()):
                    continue
                media_type = item.get("type")
                if media_type == "albums":
                    async for media in self.from_album(item["id"], select_all=True):
                        yield media
                elif media_type in {"songs", "music-videos"}:
                    media = Media.from_metadata(
                        item, api=self.api, cover_cache=self.cover_cache
                    )
                    media.source = MediaSource(kind.value, artist_id, index)
                    yield media

    async def from_library(self) -> AsyncIterator[Media]:
        """Browse and expand selected library categories lazily.

        Selecting a category never downloads its metadata up front. Only
        visited pages are fetched for individual selections; entire categories
        stream in Apple Music's page order after confirmation.
        """
        if SelectionKind.LIBRARY not in self.auto_select and self.selector is None:
            raise SelectionRequiredError(SelectionKind.LIBRARY.value)

        cache: dict[SelectionKind, PageCache[dict]] = {}

        async def get_page(
            kind: SelectionKind, index: int
        ) -> MediaPage[CollectionOption] | None:
            if kind not in LIBRARY_COLLECTIONS:
                raise ValueError("Unknown library category")
            if kind not in cache:
                endpoint = LIBRARY_COLLECTIONS[kind][0]

                async def fetch(cursor: str | None) -> MediaPage[dict]:
                    offset = int(cursor) if cursor is not None else 0
                    response = await getattr(self.api, f"get_library_{endpoint}")(
                        limit=100, offset=offset
                    )
                    items = tuple(response.get("data") or ())
                    return MediaPage(
                        items,
                        str(offset + len(items))
                        if items and response.get("next")
                        else None,
                    )

                cache[kind] = PageCache(fetch)
            pages = cache[kind]
            page = await pages.get(index)
            if page is None:
                return None
            start = pages.offset(index)
            return MediaPage(
                tuple(
                    CollectionOption(
                        start + i,
                        item.get("attributes", {}).get("name") or item["id"],
                    )
                    for i, item in enumerate(page.items)
                ),
                page.next_cursor,
            )

        if SelectionKind.LIBRARY in self.auto_select:
            selected = {kind: (None, set()) for kind in LIBRARY_COLLECTIONS}
        else:
            assert self.selector is not None
            request = LibraryBrowseRequest(
                source_id="library",
                artist_name="Your Library",
                categories=tuple(
                    (kind, label) for kind, (_, label) in LIBRARY_COLLECTIONS.items()
                ),
                load_page=get_page,
            )
            result = self.selector(request)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, ArtistBrowseResult):
                raise ValueError("Library browser must return ArtistBrowseResult")
            selected = {}
            for selection in result.selections:
                if (
                    selection.kind not in LIBRARY_COLLECTIONS
                    or selection.kind in selected
                ):
                    raise ValueError("Library browser returned an invalid category")
                collection = cache.get(selection.kind)
                visited_count = (
                    sum(len(page.items) for page in collection.pages)
                    if collection is not None
                    else 0
                )
                indices = selection.indices
                excluded = selection.excluded_indices
                if indices is not None and any(
                    type(index) is not int or not 0 <= index < visited_count
                    for index in indices
                ):
                    raise ValueError("Library browser returned an invalid item")
                if (indices is not None and excluded) or any(
                    type(index) is not int or not 0 <= index < visited_count
                    for index in excluded
                ):
                    raise ValueError("Invalid library selection exclusions")
                selected[selection.kind] = (
                    set(indices) if indices is not None else None,
                    set(excluded),
                )

        for kind in LIBRARY_COLLECTIONS:
            if kind not in selected:
                continue
            indices, excluded = selected[kind]
            # Fetch only visited pages for individual selections; selecting an
            # entire library category iterates later pages on demand.
            collection = cache.get(kind)
            if collection is None:
                await get_page(kind, 0)
                collection = cache[kind]
            if indices is None:
                resources = collection.iter_items()
            else:

                async def visited(collection=collection):
                    for page_index, page in enumerate(collection.pages):
                        start = collection.offset(page_index)
                        for offset, item in enumerate(page.items):
                            yield start + offset, item

                resources = visited()
            async for index, item in resources:
                if (indices is not None and index not in indices) or index in excluded:
                    continue
                if kind == SelectionKind.LIBRARY_ALBUMS:
                    async for media in self.from_album(
                        item["id"], is_library=True, select_all=True
                    ):
                        yield media
                elif kind == SelectionKind.LIBRARY_PLAYLISTS:
                    async for media in self.from_playlist(
                        item["id"], is_library=True, select_all=True
                    ):
                        yield media
                else:
                    media = Media.from_metadata(
                        item, api=self.api, cover_cache=self.cover_cache
                    )
                    media.source = MediaSource(kind.value, "library", index)
                    yield media

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
