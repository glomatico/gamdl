"""Search-provider adapters for the shared full-screen catalog browser.

iTunes discovery records must not be mistaken for Apple Music catalog data:
they are shown in the browser, but selected items are loaded lazily by ID.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gamdl.api import AppleMusicApi, GamdlApiResponseError, ItunesApi
from gamdl.pagination import MediaPage, PageCache

ITUNES_SEARCH_TYPES = ("songs", "music-videos")
ITUNES_MAX_RESULTS = 200
SEARCH_PAGE_SIZE = 50


def normalize_itunes_results(
    payload: Mapping[str, Any], resource_type: str
) -> tuple[dict[str, Any], ...]:
    """Provide UI-only names/IDs while retaining the original catalog type."""
    kinds = {"songs": "song", "music-videos": "music-video"}
    kind = kinds[resource_type]
    entries = payload.get("results", [])
    if not isinstance(entries, list):
        raise GamdlApiResponseError("Invalid iTunes search results")
    normalized = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != kind:
            continue
        track_id = entry.get("trackId")
        name = entry.get("trackName")
        if (
            isinstance(track_id, bool)
            or not isinstance(track_id, (int, str))
            or not str(track_id).isdigit()
            or not isinstance(name, str)
            or not name.strip()
        ):
            continue
        normalized.append(
            {
                "id": str(track_id),
                "type": resource_type,
                "attributes": {
                    "name": name,
                    "artistName": entry.get("artistName") or "",
                },
                "_search_api": "itunes",
            }
        )
    return tuple(normalized)


def create_search_caches(
    api: AppleMusicApi,
    term: str,
    resource_types: tuple[str, ...],
    *,
    search_api: str = "apple-music",
    itunes_api: ItunesApi | None = None,
) -> dict[str, PageCache[dict]]:
    """Construct lazily loaded, per-category search pages for either API."""
    if search_api not in {"apple-music", "itunes"}:
        raise ValueError("Unknown search API")
    if search_api == "itunes":
        if itunes_api is None:
            raise ValueError("iTunes search requires an ItunesApi client")
        if any(kind not in ITUNES_SEARCH_TYPES for kind in resource_types):
            raise ValueError("iTunes search only supports songs and music videos")

    caches = {}
    itunes_results: dict[str, tuple[dict[str, Any], ...]] = {}
    for resource_type in resource_types:
        if search_api == "itunes":
            entity = "song" if resource_type == "songs" else "musicVideo"

            async def load_itunes(
                cursor: str | None,
                *,
                resource_type=resource_type,
                entity=entity,
            ) -> MediaPage[dict]:
                if resource_type not in itunes_results:
                    raw = await itunes_api.get_search_results(
                        term, entity=entity, limit=ITUNES_MAX_RESULTS
                    )
                    itunes_results[resource_type] = normalize_itunes_results(
                        raw, resource_type
                    )
                items = itunes_results[resource_type]
                offset = int(cursor) if cursor is not None else 0
                page = items[offset : offset + SEARCH_PAGE_SIZE]
                next_offset = offset + SEARCH_PAGE_SIZE
                return MediaPage(
                    page,
                    str(next_offset) if next_offset < len(items) else None,
                )

            caches[resource_type] = PageCache(load_itunes)
        else:

            async def load_apple_music(
                cursor: str | None,
                *,
                resource_type=resource_type,
            ) -> MediaPage[dict]:
                response = (
                    await api.get_extended_api_data(cursor)
                    if cursor is not None
                    else await api.get_search_results(
                        term, types=resource_type, limit=SEARCH_PAGE_SIZE
                    )
                )
                groups = response.get("results")
                if not isinstance(groups, dict):
                    raise GamdlApiResponseError("Invalid Apple Music search response")
                group = groups.get(resource_type)
                if group is None:
                    if cursor is not None:
                        raise GamdlApiResponseError(
                            "Missing Apple Music search result type in pagination"
                        )
                    return MediaPage(())
                if not isinstance(group, dict) or not isinstance(
                    group.get("data"), list
                ):
                    raise GamdlApiResponseError("Invalid Apple Music search results")
                return MediaPage(tuple(group["data"]), group.get("next"))

            caches[resource_type] = PageCache(load_apple_music)

    return caches
