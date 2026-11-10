"""Credential-isolated transport for public HTTPS artwork and CDN resources."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

import httpx


class PublicTransportError(Exception):
    """Safe, URL-free explanation of a public resource request failure."""

    def __init__(
        self,
        reason: Literal[
            "invalid_url", "missing_redirect", "too_many_redirects", "request_failed"
        ],
    ) -> None:
        self.reason = reason
        super().__init__(
            {
                "invalid_url": "Expected a valid HTTPS resource URL",
                "missing_redirect": "Public resource redirect had no destination",
                "too_many_redirects": "Too many public resource redirects",
                "request_failed": "Unable to retrieve public resource",
            }[reason]
        )


class PublicTransport:
    """Fetch only public HTTPS bytes, never inheriting client auth or cookies.

    The optional HTTPX client is caller-owned, and is reused only for its
    underlying transport/connection pool. Requests are made from scratch at
    *every* redirect hop: HTTPX automatically injects client cookies when it
    constructs redirected requests, even with ``auth=None``.

    Without an injected client, an HTTPX client is created and closed for each
    request. HTTP redirects, userinfo URLs and unbounded redirect chains are
    prohibited. Network error details (including signed URLs) are not exposed.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    @staticmethod
    def _validate_url(url: str) -> None:
        try:
            parts = urlsplit(url)
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
            ):
                raise ValueError
        except ValueError:
            raise PublicTransportError("invalid_url") from None

    @classmethod
    async def _request(
        cls, client: httpx.AsyncClient, url: str, headers: dict[str, str]
    ) -> httpx.Response:
        for _ in range(11):
            cls._validate_url(url)
            response = await client.send(
                httpx.Request("GET", url, headers=headers),
                auth=None,
                follow_redirects=False,
            )
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            try:
                location = response.headers.get("location")
                if not location:
                    raise PublicTransportError("missing_redirect")
                url = str(response.url.join(location))
            finally:
                await response.aclose()
        raise PublicTransportError("too_many_redirects")

    async def fetch(
        self,
        url: str,
        *,
        byte_range: str | None = None,
        allow_not_found: bool = False,
    ) -> bytes | None:
        """Fetch response bytes, with optional 404-as-None for public artwork."""
        headers = {"Range": f"bytes={byte_range}"} if byte_range else {}
        try:
            self._validate_url(url)
            if self.client is None:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await self._request(client, url, headers)
            else:
                response = await self._request(self.client, url, headers)
            if allow_not_found and response.status_code == 404:
                return None
            response.raise_for_status()
            return response.content
        except PublicTransportError:
            raise
        except (httpx.HTTPError, httpx.InvalidURL, ValueError):
            raise PublicTransportError("request_failed") from None
