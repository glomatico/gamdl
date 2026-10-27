"""Lazy playlist metadata shared by media occurrences from one playlist."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .cache import InstanceCache
from .exceptions import GamdlMediaError

if TYPE_CHECKING:
    from gamdl.api import AppleMusicApi


@dataclass(frozen=True)
class PlaylistMetadata:
    id: str
    title: str
    artist: str | None


class Playlist:
    """One catalog/library playlist, without a dependency on the downloader.

    The resolver shares this object across all occurrences, so concurrent
    downloaders requesting its metadata use one coalesced API request.
    """

    def __init__(
        self, playlist_id: str, *, api: AppleMusicApi, is_library: bool = False
    ):
        self.id = playlist_id
        self.api = api
        self.is_library = is_library
        self._memo = InstanceCache()

    async def get_metadata(self) -> PlaylistMetadata:
        async def load() -> PlaylistMetadata:
            if self.is_library:
                response = await self.api.get_library_playlist(
                    self.id, include="catalog", limit=1
                )
            else:
                response = await self.api.get_playlist(self.id, limit_tracks=1)
            values = response.get("data") if isinstance(response, dict) else None
            if (
                not isinstance(values, list)
                or not values
                or not isinstance(values[0], dict)
            ):
                raise GamdlMediaError("Apple Music returned no playlist metadata")
            attributes = values[0].get("attributes") or {}
            title = attributes.get("name")
            artist = attributes.get("curatorName")
            return PlaylistMetadata(
                id=self.id,
                title=title if isinstance(title, str) and title else self.id,
                artist=artist if isinstance(artist, str) and artist else None,
            )

        return await self._memo.get("metadata", load)
