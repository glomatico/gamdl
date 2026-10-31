"""iTunes lookup, storefront and metadata API client."""

from __future__ import annotations

import re
from typing import Any, Self

import httpx

from .constants import (
    APPLE_MUSIC_URL,
    ITUNES_LOOKUP_URL,
    ITUNES_SEARCH_URL,
    MUSICKIT_URL,
)
from .exceptions import GamdlApiResponseError
from .http import ApiHttpClient, RequestPacer


class ItunesApi:
    """Fetch public iTunes metadata for a specific country and language.

    A country code (e.g. ``"br"``) and Apple's numeric storefront ID are
    distinct identifiers. The ID is needed by the metadata-page endpoint.
    No Apple Music user authentication is required for these requests.
    """

    def __init__(
        self,
        http: ApiHttpClient,
        *,
        storefront: str,
        storefront_id: int | None,
        language: str,
    ) -> None:
        self.http = http
        self.storefront = storefront.lower()
        self.storefront_id = storefront_id
        self.language = language

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Release the HTTP client if this API instance created it."""
        await self.http.aclose()

    @staticmethod
    async def get_storefront_id(http: ApiHttpClient, storefront: str) -> int:
        """Resolve a two-letter storefront code to Apple's numeric store ID.

        Parses the public MusicKit JavaScript mapping, which can change when
        Apple updates its frontend. The caller supplies an HTTP client.

        Raises:
            GamdlApiResponseError: Country code or numeric ID cannot be found.
        """
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
        search_only: bool = False,
    ) -> ItunesApi:
        """Create an iTunes API client for the requested storefront.

        Args:
            storefront: Two-letter country code, such as ``"br"``.
            storefront_id: Optional numeric Apple store ID. If omitted, the
                known US ID is used for ``"us"``; other countries are resolved
                from Apple's MusicKit mapping.
            language: Language tag for lookup requests.
            client: Optional caller-owned HTTPX client.
            pacer: Optional request pacer shared with other API clients.
            search_only: Skip numeric storefront resolution if only using the
                public search/lookup API. Metadata pages require a numeric ID.

        Returns:
            Initialized iTunes client; use ``async with`` to release resources.
        """
        http = ApiHttpClient("itunes", client=client, pacer=pacer, timeout=60.0)
        try:
            if storefront_id is None and not search_only:
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
        """Look up a media ID using Apple's public iTunes lookup endpoint.

        ``entity`` specifies related result types (for example ``"album"``).
        Returns the raw JSON response, including ``resultCount`` and
        ``results``; an empty result set is not treated as a request failure.
        """
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

    async def get_search_results(
        self,
        term: str,
        *,
        entity: str = "song",
        limit: int = 200,
    ) -> dict[str, Any]:
        """Search Apple's unauthenticated iTunes catalog.

        Supports ``song``, ``musicVideo`` and ``album`` for Gamdl's search browser.
        Apple documents up to 200 results per query but no cursor/offset API.
        These are discovery records, not Apple Music media metadata.
        """
        if not term.strip():
            raise ValueError("iTunes search term must not be empty")
        if entity not in {"song", "musicVideo", "album"}:
            raise ValueError("Unsupported Gamdl iTunes search entity")
        if not 1 <= limit <= 200:
            raise ValueError("iTunes search limit must be between 1 and 200")
        response = await self.http.request_json(
            "GET",
            ITUNES_SEARCH_URL,
            operation="get_search_results",
            params={
                "term": term,
                "country": self.storefront,
                "media": "musicVideo" if entity == "musicVideo" else "music",
                "entity": entity,
                "limit": limit,
            },
        )
        if not isinstance(response, dict) or not isinstance(
            response.get("results"), list
        ):
            raise GamdlApiResponseError("Invalid iTunes search response")
        return response

    async def get_itunes_page(self, media_type: str, media_id: str) -> dict[str, Any]:
        """Fetch an Apple Music metadata page with its numeric storefront ID.

        Args:
            media_type: One URL path segment identifying the content type.
            media_id: The final media identifier path segment.

        Returns:
            Raw JSON metadata response; redirects are followed.

        Raises:
            ValueError: ``media_type`` or ``media_id`` is not a safe path
                segment according to the current endpoint validation.
        """
        if self.storefront_id is None:
            raise ValueError("A numeric storefront ID is required for iTunes pages")
        if "/" in media_type or "/" in media_id or "?" in media_id or "#" in media_id:
            raise ValueError("Invalid iTunes media type or ID")
        return await self.http.request_json(
            "GET",
            f"{APPLE_MUSIC_URL}/{media_type}/{media_id}",
            operation="get_itunes_page",
            follow_redirects=True,
            headers={"X-Apple-Store-Front": f"{self.storefront_id},32 t:music31"},
        )
