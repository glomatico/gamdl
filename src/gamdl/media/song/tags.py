"""Structured song tags derived from Apple Music playback asset metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..exceptions import GamdlMediaError


@dataclass(frozen=True)
class Tags:
    """Normalized playback-asset tags, independent of output file format.

    Gamdl's v3 tagger uses the ``metadata`` mapping on the first playback
    asset, not the catalog's ``attributes`` mapping. Store optional metadata
    here without requesting iTunes, lyrics, or extra artwork. Export to MP4
    and FLAC will be implemented in the separate media processing layer.
    """

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    title_id: int | None = None
    artist_id: int | None = None
    album_id: int | None = None
    track_number: int | None = None
    track_total: int | None = None
    disc_number: int | None = None
    disc_total: int | None = None
    release_date: str | None = None
    genre: tuple[str, ...] = ()
    genre_id: int | None = None
    isrc: str | None = None
    xid: str | None = None
    title_sort: str | None = None
    artist_sort: str | None = None
    album_sort: str | None = None
    composer: str | None = None
    composer_id: int | None = None
    composer_sort: str | None = None
    copyright: str | None = None
    comment: str | None = None
    compilation: bool | None = None
    gapless: bool | None = None
    explicit: bool | None = None
    storefront: int | None = None

    @classmethod
    def from_asset_metadata(cls, metadata: dict[str, Any]) -> Tags:
        """Map Apple's playback metadata fields to neutral song tags.

        Missing fields remain unset; no catalog fields are used as a fallback.
        Apple typically represents genre as one string and IDs as strings.
        """

        if not isinstance(metadata.get("itemName"), str) or not metadata["itemName"]:
            raise GamdlMediaError("Playback asset has no song title")

        def number(key: str) -> int | None:
            value = metadata.get(key)
            return int(value) if value not in (None, "") else None

        def flag(key: str) -> bool | None:
            value = metadata.get(key)
            if isinstance(value, str):
                return value.lower() not in {"false", "0", "no", ""}
            return bool(value) if value is not None else None

        genre = metadata.get("genre")
        return cls(
            title=metadata.get("itemName"),
            artist=metadata.get("artistName"),
            album=metadata.get("playlistName"),
            album_artist=metadata.get("playlistArtistName"),
            title_id=number("itemId"),
            artist_id=number("artistId"),
            album_id=number("playlistId"),
            track_number=number("trackNumber"),
            track_total=number("trackCount"),
            disc_number=number("discNumber"),
            disc_total=number("discCount"),
            release_date=metadata.get("releaseDate"),
            genre=(genre,) if isinstance(genre, str) and genre else (),
            genre_id=number("genreId"),
            isrc=metadata.get("isrc"),
            xid=metadata.get("xid"),
            title_sort=metadata.get("sort-name"),
            artist_sort=metadata.get("sort-artist"),
            album_sort=metadata.get("sort-album"),
            composer=metadata.get("composerName"),
            composer_id=number("composerId"),
            composer_sort=metadata.get("sort-composer"),
            copyright=metadata.get("copyright"),
            comment=metadata.get("comments"),
            compilation=flag("compilation"),
            gapless=flag("gapless"),
            explicit=flag("explicit"),
            storefront=number("s"),
        )
