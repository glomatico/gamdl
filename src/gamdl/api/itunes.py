"""iTunes lookup, storefront and metadata API client."""

from __future__ import annotations

import re
from typing import Any

import httpx

from .constants import APPLE_MUSIC_URL, ITUNES_LOOKUP_URL, MUSICKIT_URL
from .exceptions import GamdlApiResponseError
from .http import ApiHttpClient, RequestPacer


class ItunesApi:
    """The iTunes metadata API, independent of the Apple Music auth session."""

    def __init__(
        self,
        http: ApiHttpClient,
        *,
        storefront: str,
        storefront_id: int,
        language: str,
    ) -> None:
        self.http = http
        self.storefront = storefront.lower()
        self.storefront_id = storefront_id
        self.language = language

    async def __aenter__(self) -> ItunesApi:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.http.aclose()

    @staticmethod
    async def get_storefront_id(http: ApiHttpClient, storefront: str) -> int:
        """Discover numeric storefront from MusicKit without assuming US IDs."""
        js = await http.get_text(MUSICKIT_URL, operation="resolve_storefront")
        country = re.search(rf'{re.escape(storefront.upper())}:"([A-Z]{{3}})"', js)
        if country is None:
            raise GamdlApiResponseError("Storefront country not found in MusicKit data")
        numeric = re.search(rf'{re.escape(country.group(1))}:"(\d+)"', js)
        if numeric is None:
            raise GamdlApiResponseError("Storefront ID not found in MusicKit data")
        return int(numeric.group(1))

    @classmethod
    async def create(
        cls,
        *,
        storefront: str = "us",
        storefront_id: int | None = None,
        language: str = "en-US",
        client: httpx.AsyncClient | None = None,
        pacer: RequestPacer | None = None,
    ) -> ItunesApi:
        http = ApiHttpClient("itunes", client=client, pacer=pacer, timeout=60.0)
        try:
            if storefront_id is None:
                storefront_id = (
                    143441
                    if storefront.lower() == "us"
                    else await cls.get_storefront_id(http, storefront)
                )
            return cls(
                http,
                storefront=storefront,
                storefront_id=storefront_id,
                language=language,
            )
        except BaseException:
            await http.aclose()
            raise

    async def get_lookup_result(
        self, media_id: str, *, entity: str = "album"
    ) -> dict[str, Any]:
        return await self.http.request_json(
            "GET",
            ITUNES_LOOKUP_URL,
            operation="get_lookup_result",
            params={
                "id": media_id,
                "entity": entity,
                "country": self.storefront,
                "lang": self.language,
            },
        )

    async def get_itunes_page(self, media_type: str, media_id: str) -> dict[str, Any]:
        # Only allow a single path segment in user-supplied media IDs and types.
        if "/" in media_type or "/" in media_id or "?" in media_id or "#" in media_id:
            raise ValueError("Invalid iTunes media type or ID")
        return await self.http.request_json(
            "GET",
            f"{APPLE_MUSIC_URL}/{media_type}/{media_id}",
            operation="get_itunes_page",
            follow_redirects=True,
            headers={"X-Apple-Store-Front": f"{self.storefront_id},32 t:music31"},
        )
