"""Shared, bounded asynchronous cache of downloaded artwork bytes."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
from async_lru import alru_cache

from .exceptions import GamdlMediaError


class _ArtworkNotFound(Exception):
    """Private signal: 404 results should not occupy cache entries."""


async def fetch_cover_bytes(
    url: str, *, client: httpx.AsyncClient | None = None
) -> bytes | None:
    """Fetch public HTTPS artwork without forwarding Apple Music credentials.

    Requests are made only on explicit byte retrieval. A supplied HTTPX client
    remains caller-owned. Errors never include artwork URLs or response bodies.
    """
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username:
            raise GamdlMediaError("Expected a valid HTTPS artwork URL")
    except ValueError:
        raise GamdlMediaError("Expected a valid HTTPS artwork URL") from None

    try:
        if client is None:
            async with httpx.AsyncClient(timeout=30.0) as owned:
                response = await owned.get(url, follow_redirects=True)
        else:
            # Build an isolated request to avoid inheriting cookies, headers,
            # or default authentication from a caller-provided client.
            response = await client.send(
                httpx.Request("GET", url), auth=None, follow_redirects=True
            )
        if response.status_code == 404:
            return None
        response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        raise GamdlMediaError("Unable to retrieve artwork") from None
    return response.content


class CoverCache:
    """LRU artwork-byte cache shared by covers, keyed by formatted URL.

    Distinct image dimensions, formats or query parameters have different
    entries. The limit is by entry count, not memory bytes. Concurrent cache
    misses for one URL share the same download, and failed/404 requests are
    never cached. No HTTP client is created until the first cache miss.
    """

    def __init__(
        self,
        *,
        maxsize: int = 64,
        ttl: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if maxsize < 1:
            raise ValueError("Cover cache maxsize must be positive")
        self._client = client
        self._cached = alru_cache(maxsize=maxsize, ttl=ttl)(self._fetch)

    async def _fetch(self, url: str) -> bytes:
        result = await fetch_cover_bytes(url, client=self._client)
        if result is None:
            raise _ArtworkNotFound
        return result

    async def get_bytes(self, url: str) -> bytes | None:
        try:
            return await self._cached(url)
        except _ArtworkNotFound:
            return None

    def clear(self) -> None:
        """Forget completed cached artwork while preserving the cache owner."""
        self._cached.cache_clear()

    async def aclose(self) -> None:
        """Cancel pending cache loads and release cached bytes.

        The optionally supplied HTTPX client is owned and closed by its caller.
        """
        await self._cached.cache_close()
