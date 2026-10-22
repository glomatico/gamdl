"""Common song tags independent of any output file format."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Tags:
    """Core catalog song metadata; format-specific tag writing comes later."""

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    track_number: int | None = None
    disc_number: int | None = None
    release_date: str | None = None
    genre: tuple[str, ...] = ()
    isrc: str | None = None

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any]) -> Tags:
        attrs = metadata.get("attributes", {})
        genres = attrs.get("genreNames")
        return cls(
            title=attrs.get("name"),
            artist=attrs.get("artistName"),
            album=attrs.get("albumName"),
            album_artist=attrs.get("albumArtistName"),
            track_number=attrs.get("trackNumber"),
            disc_number=attrs.get("discNumber"),
            release_date=attrs.get("releaseDate"),
            genre=tuple(genres) if isinstance(genres, list) else (),
            isrc=attrs.get("isrc"),
        )
