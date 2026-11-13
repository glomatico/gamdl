"""Output-oriented tags parsed from playback and iTunes metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .exceptions import TagResolutionError

DEFAULT_DATE_TAG_TEMPLATE = "%Y-%m-%dT%H:%M:%SZ"


def format_date_tag(date: str | None, template: str) -> str | None:
    """Format Apple's ISO release date for a file tag, not paths or the index.

    Apple can supply a full timestamp, a date, or a partial year/month. Keep
    unrecognized metadata intact instead of failing the entire download.
    """
    if not isinstance(template, str) or not template.strip():
        raise ValueError("date_tag_template must be a nonempty strftime format")
    if date is None:
        return None
    value = date
    if re.fullmatch(r"\d{4}", value):
        value += "-01-01"
    elif re.fullmatch(r"\d{4}-\d{2}", value):
        value += "-01"
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        return date
    return parsed.strftime(template)


@dataclass(frozen=True)
class Tags:
    """Normalized playback-asset tags for songs and music videos.

    Asset metadata may be provided by Webplayback or a wrapper, and is
    distinct from catalog attributes and file-container tagging.
    Parsing this model performs no network requests.
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
    media_type: int | None = None
    rating: int | None = None
    lyrics: str | None = None

    @classmethod
    def from_asset_metadata(cls, metadata: dict[str, Any]) -> Tags:
        """Map Apple's playback metadata fields to neutral media tags.

        Missing fields remain unset; no catalog fields are used as a fallback.
        Apple typically represents genre as one string and IDs as strings.
        """

        if not isinstance(metadata.get("itemName"), str) or not metadata["itemName"]:
            raise TagResolutionError("Playback asset has no media title")

        def number(key: str) -> int | None:
            value = metadata.get(key)
            if value in (None, ""):
                return None
            try:
                return int(value)
            except (ValueError, TypeError):
                raise TagResolutionError(
                    "Playback asset contains an invalid numeric tag"
                ) from None

        def flag(key: str) -> bool | None:
            value = metadata.get(key)
            if isinstance(value, str):
                return value.lower() not in {"false", "0", "no", ""}
            return bool(value) if value is not None else None

        genre = metadata.get("genre")
        kind = metadata.get("kind")
        explicit = metadata.get("explicit")
        explicitness = (
            int(explicit)
            if isinstance(explicit, (int, str)) and str(explicit).isdigit()
            else None
        )
        return cls(
            title=metadata.get("itemName"),
            artist=metadata.get("artistName"),
            album=metadata.get("playlistName"),
            album_artist=metadata.get("playlistArtistName"),
            title_id=(
                value if (value := number("itemId")) is not None and value > 0 else None
            ),
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
            media_type=6 if kind == "music-video" else 1 if kind == "song" else None,
            rating=explicitness,
        )
