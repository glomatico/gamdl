"""Lazy resolution of direct media, albums, playlists and Apple Music URLs."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from .base import Media, MediaSource
from .constants import DEFAULT_AUTO_SELECT
from .enums import SelectionKind
from .exceptions import SelectionRequiredError, UnsupportedMediaError
from .models import SelectionOption, SelectionRequest
from .song import Song
from .music_video import MusicVideo
from .uploaded_video import UploadedVideo

if TYPE_CHECKING:
    from gamdl.api import AppleMusicApi


Selector = Callable[[SelectionRequest], Sequence[int] | Awaitable[Sequence[int]]]


class MediaResolver:
    """Resolve source IDs/URLs to a stream of lazy media objects.

    Album and playlist tracks are selected automatically by default. Override
    ``auto_select`` to require an explicit ``selector`` for either source. A
    selector returns occurrence indices, preserving order and duplicate IDs.

    Artist and library selection will be implemented in the next resolver
    milestone; they currently raise a specific error rather than guessing.
    """

    def __init__(
        self,
        api: AppleMusicApi,
        *,
        selector: Selector | None = None,
        auto_select: Iterable[SelectionKind | str] = DEFAULT_AUTO_SELECT,
    ) -> None:
        self.api = api
        self.selector = selector
        self.auto_select = frozenset(SelectionKind(item) for item in auto_select)

    async def from_song(
        self, song_id: str, *, is_library: bool = False
    ) -> AsyncIterator[Media]:
        """Yield one unloaded song; no remote calls are required."""
        yield Song.from_id(song_id, api=self.api, is_library=is_library)

    async def from_music_video(
        self, media_id: str, *, is_library: bool = False
    ) -> AsyncIterator[Media]:
        """Yield one unloaded music video."""
        yield MusicVideo.from_id(media_id, api=self.api, is_library=is_library)

    async def from_uploaded_video(self, media_id: str) -> AsyncIterator[Media]:
        """Yield one unloaded uploaded video."""
        yield UploadedVideo.from_id(media_id, api=self.api)

    async def _from_collection(
        self,
        source_id: str,
        kind: SelectionKind,
        resources: AsyncIterator[dict],
    ) -> AsyncIterator[Media]:
        """Stream auto-selected resources, materialize only for explicit UI selection."""
        if kind not in self.auto_select and self.selector is None:
            raise SelectionRequiredError(kind.value)

        def as_media(metadata: dict, index: int) -> Media:
            media = Media.from_metadata(metadata, api=self.api)
            media.source = MediaSource(kind=kind.value, id=source_id, index=index)
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
        if inspect.isawaitable(selected):
            selected = await selected
        if isinstance(selected, (str, bytes)) or not isinstance(selected, Sequence):
            raise ValueError("Selector must return a sequence of occurrence indices")
        if any(type(i) is not int or i < 0 or i >= len(options) for i in selected):
            raise ValueError("Selector returned an invalid occurrence index")
        indices = set(selected)
        for option in options:
            if option.index in indices:
                yield option.media

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
        async for media in self._from_collection(
            playlist_id, SelectionKind.PLAYLIST_TRACKS, resources
        ):
            yield media

    async def from_artist(self, artist_id: str) -> AsyncIterator[Media]:
        """Reserve artist selection for the next phase; never select blindly."""
        if SelectionKind.ARTIST not in self.auto_select and self.selector is None:
            raise SelectionRequiredError(SelectionKind.ARTIST.value)
        raise UnsupportedMediaError("Artist selections are not implemented yet")
        yield  # Keep this an async iterator, consistently with other sources.

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
