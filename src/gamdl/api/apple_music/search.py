"""Catalog search requests and lazy per-type result pagination."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..exceptions import GamdlApiResponseError


class SearchEndpoints:
    """Grouped search endpoints, sharing the client's safe pager."""

    async def get_search_results(
        self,
        term: str,
        *,
        types: str = "songs,music-videos,albums,playlists,artists",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Fetch one page of catalog search results grouped by resource type.

        Args:
            term: Search query.
            types: Comma-separated Apple Music resource types to search.
            limit: Requested number of results per type on this page.
            offset: Starting offset for the initial search query.

        Returns:
            Raw search response with ``results`` grouped by type. Each group
            may contain its own ``next`` link for additional results.

        See Also:
            :meth:`iter_search_results` to consume every page automatically.
        """
        return await self._get(
            f"/v1/catalog/{self.storefront}/search",
            operation="get_search_results",
            params={"term": term, "types": types, "limit": limit, "offset": offset},
        )

    async def iter_search_results(
        self,
        term: str,
        *,
        types: str = "songs,music-videos,albums,playlists,artists",
        limit: int = 50,
        offset: int = 0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield catalog search results, following pagination for each type.

        Apple's search response contains separate groups such as ``songs`` and
        ``albums``, each with an independent ``next`` cursor. This method
        exhausts one type before moving to the next, in the order specified
        by ``types``. It does **not** provide a global relevance ranking across
        types. Missing/empty groups yield nothing.

        Args:
            term: Search query.
            types: Comma-separated resource types, in preferred output order.
            limit: Requested page size per resource type, not a total cap.
            offset: Starting offset for the initial search request.

        Yields:
            Raw resource dictionaries with their original ``type`` field.

        Notes:
            Pages are fetched only as needed. Breaking out of ``async for``
            avoids requesting additional pages.
        """
        page = await self.get_search_results(
            term, types=types, limit=limit, offset=offset
        )
        results = page.get("results")
        if not isinstance(results, dict):
            raise GamdlApiResponseError("Apple Music search response has no results")
        for result_type in dict.fromkeys(
            t.strip() for t in types.split(",") if t.strip()
        ):
            if result_type not in results:
                continue
            async for item in self._iterate_pages(page, result_type=result_type):
                yield item
