"""Authenticated Apple Music client, lifecycle and shared AMP transport."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from gamdl.transport import ApiHttpClient, RequestPacer

from ..auth import (
    AppleMusicCredentials,
    credentials_from_wrapper,
    fetch_developer_token,
    media_user_token_from_cookies,
)
from ..constants import APPLE_MUSIC_ACCOUNT_URL, APPLE_MUSIC_API_URL, APPLE_MUSIC_URL
from ..exceptions import GamdlApiResponseError, GamdlAuthenticationError
from ..wrapper import WrapperApi
from .catalog import CatalogEndpoints
from .drm import DrmEndpoints
from .library import LibraryEndpoints
from .playback import PlaybackEndpoints
from .search import SearchEndpoints


class AppleMusicApi(
    CatalogEndpoints, LibraryEndpoints, SearchEndpoints, PlaybackEndpoints, DrmEndpoints
):
    """Access Apple Music catalog, library, search, and playback endpoints.

    Endpoint methods return the unmodified JSON response as a dictionary.
    ``iter_*`` methods instead yield individual resource dictionaries, fetching
    additional pages on demand. API calls are not made until an iterator is
    consumed.

    Create instances with ``create()``, ``from_cookies()``, or
    ``from_wrapper()``. Use them as asynchronous context managers to release
    internally owned connections. Injected HTTPX clients remain caller-owned.
    """

    def __init__(
        self,
        http: ApiHttpClient,
        credentials: AppleMusicCredentials,
        storefront: str,
        language: str,
        account_info: dict[str, Any] | None = None,
    ) -> None:
        self.http = http
        self.credentials = credentials
        self.storefront = storefront.lower()
        self.language = language
        self.account_info = account_info

    async def __aenter__(self) -> AppleMusicApi:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client only if this API instance owns it."""
        await self.http.aclose()

    @property
    def active_subscription(self) -> bool:
        """Whether account metadata confirms an active Apple Music subscription.

        Returns ``False`` if the API instance has no authenticated account
        information, which is not proof that the account lacks a subscription.
        """
        return bool(
            (self.account_info or {})
            .get("meta", {})
            .get("subscription", {})
            .get("active")
        )

    @property
    def account_restrictions(self) -> dict[str, Any] | None:
        """Return account content restrictions, if present in account metadata."""
        data = (self.account_info or {}).get("data") or []
        return data[0].get("attributes", {}).get("restrictions") if data else None

    @property
    def _headers(self) -> dict[str, str]:
        headers = {
            "authorization": f"Bearer {self.credentials.developer_token}",
            "origin": APPLE_MUSIC_URL,
        }
        if self.credentials.media_user_token:
            headers["cookie"] = f"media-user-token={self.credentials.media_user_token}"
        return headers

    @classmethod
    async def create(
        cls,
        *,
        storefront: str | None = None,
        language: str = "en-US",
        token: str | None = None,
        media_user_token: str | None = None,
        client: httpx.AsyncClient | None = None,
        pacer: RequestPacer | None = None,
    ) -> AppleMusicApi:
        """Create a client from tokens, optionally discovering missing credentials.

        Args:
            storefront: Two-letter country code. Defaults to the authenticated
                account's storefront when available, otherwise ``"us"``.
            language: Language tag used for web playback requests.
            token: Apple Music developer token. If omitted, attempt to extract
                one from Apple's web player.
            media_user_token: Optional authenticated user's token. When given,
                validate it with the account endpoint during creation.
            client: Optional externally managed HTTPX client, useful for tests.
                The caller is responsible for closing this client.
            pacer: Optional shared limiter for serializing Apple API requests.

        Returns:
            An initialized API instance, suitable for ``async with``.

        Raises:
            GamdlApiError: If token discovery or account validation fails.
        """
        http = ApiHttpClient("apple_music", client=client, pacer=pacer)
        try:
            token = token or await fetch_developer_token(http)
            credentials = AppleMusicCredentials(token, media_user_token)
            account_info = None
            if media_user_token:
                headers = {
                    "authorization": f"Bearer {token}",
                    "origin": APPLE_MUSIC_URL,
                    "cookie": f"media-user-token={media_user_token}",
                }
                account_info = await http.request_json(
                    "GET",
                    APPLE_MUSIC_ACCOUNT_URL,
                    operation="get_account_info",
                    params={"meta": "subscription"},
                    headers=headers,
                )
                if account_info.get("errors"):
                    raise GamdlAuthenticationError(
                        "Apple Music account validation failed"
                    )

            detected_storefront = (
                (account_info or {})
                .get("meta", {})
                .get("subscription", {})
                .get("storefront")
            )
            return cls(
                http,
                credentials,
                storefront or detected_storefront or "us",
                language,
                account_info,
            )
        except BaseException:
            await http.aclose()
            raise

    @classmethod
    async def from_cookies(
        cls,
        path: str,
        **kwargs: Any,
    ) -> AppleMusicApi:
        """Create an authenticated client using a Netscape cookies file.

        The file must contain ``media-user-token`` for ``music.apple.com``.
        Additional keyword arguments are forwarded to :meth:`create`.
        Cookies are read from disk but never migrated or persisted by this API.
        """
        return await cls.create(
            media_user_token=media_user_token_from_cookies(path),
            **kwargs,
        )

    @classmethod
    async def from_wrapper(
        cls,
        wrapper: WrapperApi,
        **kwargs: Any,
    ) -> AppleMusicApi:
        """Create a client using authentication tokens from an active wrapper.

        ``wrapper.me`` must contain both the developer and music-user tokens.
        Additional keyword arguments are forwarded to :meth:`create`.
        """
        credentials = credentials_from_wrapper(wrapper.me)
        return await cls.create(
            token=credentials.developer_token,
            media_user_token=credentials.media_user_token,
            **kwargs,
        )

    @staticmethod
    def _amp_url(path: str) -> str:
        """Only send Apple Music credentials to the AMP host."""
        url = urljoin(APPLE_MUSIC_API_URL + "/", path)
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "amp-api.music.apple.com":
            raise GamdlApiResponseError("Invalid Apple Music API pagination URL")
        return url

    async def _get(
        self,
        path: str,
        *,
        operation: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch one AMP API response and reject top-level API errors."""
        result = await self.http.request_json(
            "GET",
            self._amp_url(path),
            operation=operation,
            params=params,
            headers=self._headers,
        )
        if result.get("errors"):
            raise GamdlApiResponseError(
                f"Apple Music API reported errors during {operation}",
                service="apple_music",
            )
        return result

    async def _iterate_pages(
        self,
        page: dict[str, Any],
        *,
        result_type: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate ``data``/``next`` pages, or one nested search result group.

        For grouped searches, every fetched page must contain
        ``results[result_type]``. Repeated pagination cursors and invalid
        collection shapes raise :class:`GamdlApiResponseError` rather than
        silently skipping results or looping indefinitely.
        """
        seen: set[str] = set()
        while True:
            collection = page
            if result_type is not None:
                results = page.get("results")
                if not isinstance(results, dict):
                    raise GamdlApiResponseError(
                        "Apple Music search pagination response has no results object"
                    )
                collection = results.get(result_type)
                if not isinstance(collection, dict):
                    raise GamdlApiResponseError(
                        "Apple Music search pagination response has no requested type"
                    )

            data = collection.get("data")
            if not isinstance(data, list):
                raise GamdlApiResponseError(
                    "Apple Music API pagination response has no data list"
                )
            for item in data:
                yield item
            next_url = collection.get("next")
            if next_url is None:
                return
            if not isinstance(next_url, str) or not next_url or next_url in seen:
                raise GamdlApiResponseError("Invalid Apple Music API pagination cursor")
            seen.add(next_url)
            page = await self.get_extended_api_data(next_url)

    async def get_extended_api_data(self, next_uri: str) -> dict[str, Any]:
        """Fetch a server-provided pagination link from the AMP API.

        Only HTTPS links targeting ``amp-api.music.apple.com`` are allowed;
        this prevents forwarding authentication headers to another host.
        ``next_uri`` may be a relative URI or an absolute AMP URL.
        """
        return await self._get(next_uri, operation="get_extended_api_data")
