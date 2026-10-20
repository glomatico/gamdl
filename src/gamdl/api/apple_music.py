"""Asynchronous Apple Music catalog, library and playback API client."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from ..logging import get_logger
from .auth import (
    AppleMusicCredentials,
    credentials_from_wrapper,
    fetch_developer_token,
    media_user_token_from_cookies,
)
from .constants import (
    APPLE_MUSIC_ACCOUNT_URL,
    APPLE_MUSIC_API_URL,
    APPLE_MUSIC_LICENSE_URL,
    APPLE_MUSIC_URL,
    APPLE_MUSIC_WEBPLAYBACK_URL,
)
from .exceptions import GamdlApiResponseError, GamdlAuthenticationError
from .http import ApiHttpClient, RequestPacer
from .wrapper import WrapperApi

logger = get_logger(__name__)


class AppleMusicApi:
    """High-level endpoint methods returning raw JSON dictionaries.

    Clients created with ``create`` or ``from_cookies`` own their internally
    created HTTPX connection and can be used with ``async with``. Injected HTTPX
    clients remain owned by their callers.
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
        await self.http.aclose()

    @property
    def active_subscription(self) -> bool:
        return bool(
            (self.account_info or {})
            .get("meta", {})
            .get("subscription", {})
            .get("active")
        )

    @property
    def account_restrictions(self) -> dict[str, Any] | None:
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

    async def get_song(
        self,
        song_id: str,
        *,
        extend: str = "extendedAssetUrls",
        include: str = "lyrics,albums",
    ) -> dict[str, Any]:
        return await self._get(
            f"/v1/catalog/{self.storefront}/songs/{song_id}",
            operation="get_song",
            params={"extend": extend, "include": include},
        )

    async def get_music_video(
        self, music_video_id: str, *, include: str = "albums"
    ) -> dict[str, Any]:
        return await self._get(
            f"/v1/catalog/{self.storefront}/music-videos/{music_video_id}",
            operation="get_music_video",
            params={"include": include},
        )

    async def get_uploaded_video(self, uploaded_video_id: str) -> dict[str, Any]:
        return await self._get(
            f"/v1/catalog/{self.storefront}/uploaded-videos/{uploaded_video_id}",
            operation="get_uploaded_video",
        )

    async def get_album(
        self, album_id: str, *, extend: str = "extendedAssetUrls"
    ) -> dict[str, Any]:
        return await self._get(
            f"/v1/catalog/{self.storefront}/albums/{album_id}",
            operation="get_album",
            params={"extend": extend},
        )

    async def get_playlist(
        self,
        playlist_id: str,
        *,
        limit_tracks: int = 300,
        extend: str = "extendedAssetUrls",
    ) -> dict[str, Any]:
        return await self._get(
            f"/v1/catalog/{self.storefront}/playlists/{playlist_id}",
            operation="get_playlist",
            params={"limit[tracks]": limit_tracks, "extend": extend},
        )

    async def get_artist(
        self,
        artist_id: str,
        *,
        include: str = "albums,music-videos",
        views: str = "full-albums,compilation-albums,live-albums,singles,top-songs",
        limit: int = 100,
    ) -> dict[str, Any]:
        return await self._get(
            f"/v1/catalog/{self.storefront}/artists/{artist_id}",
            operation="get_artist",
            params={
                "include": include,
                "views": views,
                **{
                    f"limit[{section}]": limit
                    for section in [*include.split(","), *views.split(",")]
                },
            },
        )

    async def get_library_song(
        self,
        song_id: str,
        *,
        include: str = "catalog",
        extend: str = "extendedAssetUrls",
    ) -> dict[str, Any]:
        return await self._get(
            f"/v1/me/library/songs/{song_id}",
            operation="get_library_song",
            params={"include": include, "extend": extend},
        )

    async def get_library_music_video(
        self, music_video_id: str, *, include: str = "catalog"
    ) -> dict[str, Any]:
        return await self._get(
            f"/v1/me/library/music-videos/{music_video_id}",
            operation="get_library_music_video",
            params={"include": include},
        )

    async def get_library_album(
        self,
        album_id: str,
        *,
        include: str = "catalog",
        extend: str = "extendedAssetUrls",
    ) -> dict[str, Any]:
        return await self._get(
            f"/v1/me/library/albums/{album_id}",
            operation="get_library_album",
            params={"include": include, "extend": extend},
        )

    async def get_library_playlist(
        self,
        playlist_id: str,
        *,
        include: str = "catalog,tracks",
        limit: int = 100,
        extend: str = "extendedAssetUrls",
    ) -> dict[str, Any]:
        return await self._get(
            f"/v1/me/library/playlists/{playlist_id}",
            operation="get_library_playlist",
            params={
                "include": include,
                **{f"limit[{section}]": limit for section in include.split(",")},
                "extend": extend,
            },
        )

    async def _library_list(
        self,
        type_name: str,
        *,
        limit: int,
        offset: int,
        include: str,
        extend: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "offset": offset, "include": include}
        if extend is not None:
            params["extend"] = extend
        return await self._get(
            f"/v1/me/library/{type_name}",
            operation=f"get_library_{type_name.replace('-', '_')}",
            params=params,
        )

    async def get_library_songs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        include: str = "catalog",
        extend: str = "extendedAssetUrls",
    ) -> dict[str, Any]:
        return await self._library_list(
            "songs", limit=limit, offset=offset, include=include, extend=extend
        )

    async def get_library_music_videos(
        self, *, limit: int = 100, offset: int = 0, include: str = "catalog"
    ) -> dict[str, Any]:
        return await self._library_list(
            "music-videos", limit=limit, offset=offset, include=include
        )

    async def get_library_albums(
        self, *, limit: int = 100, offset: int = 0, include: str = "catalog"
    ) -> dict[str, Any]:
        return await self._library_list(
            "albums", limit=limit, offset=offset, include=include
        )

    async def get_library_playlists(
        self, *, limit: int = 100, offset: int = 0, include: str = "catalog"
    ) -> dict[str, Any]:
        return await self._library_list(
            "playlists", limit=limit, offset=offset, include=include
        )

    async def iter_library_playlists(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        include: str = "catalog",
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield all user playlists in server order, transparently following `next`."""
        page = await self.get_library_playlists(
            limit=limit, offset=offset, include=include
        )
        async for item in self._iterate_pages(page):
            yield item

    async def iter_library_songs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        include: str = "catalog",
        extend: str = "extendedAssetUrls",
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield the user's songs across all pages, in library order."""
        page = await self.get_library_songs(
            limit=limit, offset=offset, include=include, extend=extend
        )
        async for item in self._iterate_pages(page):
            yield item

    async def iter_library_music_videos(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        include: str = "catalog",
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield all music videos in the user's library."""
        page = await self.get_library_music_videos(
            limit=limit, offset=offset, include=include
        )
        async for item in self._iterate_pages(page):
            yield item

    async def iter_library_albums(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        include: str = "catalog",
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield all albums in the user's library."""
        page = await self.get_library_albums(
            limit=limit, offset=offset, include=include
        )
        async for item in self._iterate_pages(page):
            yield item

    async def _iterate_pages(
        self,
        page: dict[str, Any],
        *,
        result_type: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate plain collections or type-grouped search result pages."""
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
        return await self._get(next_uri, operation="get_extended_api_data")

    async def get_search_results(
        self,
        term: str,
        *,
        types: str = "songs,music-videos,albums,playlists,artists",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
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
        """Yield catalog search resources across pages for each requested type.

        Apple returns independent result groups per type, each with its own
        pagination cursor. Groups are emitted in the order given in ``types``;
        results within a group preserve Apple's ordering. An absent group
        simply means no results for that type.
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

    async def iter_playlist_tracks(
        self,
        playlist_id: str,
        *,
        is_library: bool = False,
        limit: int = 100,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every track of a catalog or user-library playlist in order."""
        base = (
            f"/v1/me/library/playlists/{playlist_id}"
            if is_library
            else f"/v1/catalog/{self.storefront}/playlists/{playlist_id}"
        )
        page = await self._get(
            f"{base}/tracks",
            operation="iter_playlist_tracks",
            params={"limit": limit},
        )
        async for item in self._iterate_pages(page):
            yield item

    async def iter_album_tracks(
        self,
        album_id: str,
        *,
        is_library: bool = False,
        limit: int = 100,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every track in a catalog or user-library album in order."""
        base = (
            f"/v1/me/library/albums/{album_id}"
            if is_library
            else f"/v1/catalog/{self.storefront}/albums/{album_id}"
        )
        page = await self._get(
            f"{base}/tracks",
            operation="iter_album_tracks",
            params={"limit": limit},
        )
        async for item in self._iterate_pages(page):
            yield item

    async def iter_artist_view(
        self,
        artist_id: str,
        view: str,
        *,
        limit: int = 100,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield a catalog artist's view, e.g. ``full-albums`` or ``top-songs``."""
        if not view or any(c in view for c in "/?#"):
            raise ValueError("Invalid artist view")
        page = await self._get(
            f"/v1/catalog/{self.storefront}/artists/{artist_id}/view/{view}",
            operation="iter_artist_view",
            params={"limit": limit},
        )
        async for item in self._iterate_pages(page):
            yield item

    async def get_assets(
        self,
        media_id: str,
        *,
        kind: str = "song",
        include_license_urls: bool = True,
        hls_encryption: str = "CBC",
        hls_profile: str = "enhancedHls",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "id": media_id,
            "kind": kind,
            "includeLicenseUrls": include_license_urls,
            "hlsEncryption": hls_encryption,
        }
        if hls_profile:
            params["hlsProfile"] = hls_profile
        return await self._get("/v1/play/assets", operation="get_assets", params=params)

    async def get_webplayback(
        self, track_id: str, *, is_library: bool = False
    ) -> dict[str, Any]:
        body = {
            ("universalLibraryId" if is_library else "salableAdamId"): track_id,
            "language": self.language,
        }
        response = await self.http.request_json(
            "POST",
            APPLE_MUSIC_WEBPLAYBACK_URL,
            operation="get_webplayback",
            json=body,
            headers=self._headers,
        )
        if response.get("dialog"):
            raise GamdlApiResponseError("Apple Music web playback was rejected")
        return response

    async def get_license_exchange(
        self,
        track_id: str,
        track_uri: str,
        challenge: str,
        *,
        key_system: str = "com.widevine.alpha",
        is_library: bool = False,
    ) -> dict[str, Any]:
        response = await self.http.request_json(
            "POST",
            APPLE_MUSIC_LICENSE_URL,
            operation="get_license_exchange",
            headers=self._headers,
            json={
                "challenge": challenge,
                "key-system": key_system,
                "uri": track_uri,
                "adamId": track_id,
                "isLibrary": is_library,
                "user-initiated": True,
            },
        )
        if response.get("status") != 0:
            raise GamdlApiResponseError("Apple Music license request was rejected")
        return response
