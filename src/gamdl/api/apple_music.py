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

    async def get_song(
        self,
        song_id: str,
        *,
        extend: str = "extendedAssetUrls",
        include: str = "lyrics,albums",
    ) -> dict[str, Any]:
        """Fetch one catalog song, optionally including lyrics and album data."""
        return await self._get(
            f"/v1/catalog/{self.storefront}/songs/{song_id}",
            operation="get_song",
            params={"extend": extend, "include": include},
        )

    async def get_lyrics(
        self,
        song_id: str,
        *,
        language: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a catalog song's standard lyrics resource.

        Uses the AMP ``/songs/{id}/lyrics`` endpoint rather than relying on
        the optional ``lyrics`` relationship included by :meth:`get_song`.
        The returned JSON usually contains TTML in
        ``data[0]["attributes"]["ttml"]``; the caller can decide how to
        parse or convert the document.

        Args:
            song_id: Catalog song identifier, not a personal-library ID.
            language: Optional Apple Music localization tag (``l`` parameter).

        Returns:
            Raw lyrics API response, including its ``data`` resources.

        Notes:
            Lyrics availability and access depend on the track, storefront,
            and account authorization. This is a private Apple Music endpoint
            and may change independently of the supported catalog API.
        """
        return await self._get(
            f"/v1/catalog/{self.storefront}/songs/{song_id}/lyrics",
            operation="get_lyrics",
            params={"l": language} if language is not None else None,
        )

    async def get_syllable_lyrics(
        self,
        song_id: str,
        *,
        language: str | None = None,
        lyrics_language: str | None = None,
        script: str | None = None,
        extend: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a catalog song's syllable- or word-timed lyrics resource.

        Uses the AMP ``/songs/{id}/syllable-lyrics`` endpoint. Apple may
        return TTML with timestamped ``<span>`` elements, supporting
        syllable-level highlighting. The response is returned unchanged;
        converting TTML to LRC/SRT belongs to a separate lyrics processor.

        Args:
            song_id: Catalog song identifier, not a personal-library ID.
            language: Optional general localization tag (``l``).
            lyrics_language: Optional lyrics-specific locale (``l[lyrics]``).
            script: Optional lyrics script preference (``l[script]``), such as
                ``"en-Latn"``.
            extend: Optional extension, e.g. ``"ttmlLocalizations"`` to
                request available translated/transliterated lyric metadata.

        Returns:
            Raw JSON response, typically with TTML at
            ``data[0]["attributes"]["ttml"]`` or localized lyric fields.

        Notes:
            Not all songs have syllable timing. This is a private endpoint;
            it may return an error for tracks without accessible lyrics.
        """
        params: dict[str, str] = {}
        if language is not None:
            params["l"] = language
        if lyrics_language is not None:
            params["l[lyrics]"] = lyrics_language
        if script is not None:
            params["l[script]"] = script
        if extend is not None:
            params["extend"] = extend

        return await self._get(
            f"/v1/catalog/{self.storefront}/songs/{song_id}/syllable-lyrics",
            operation="get_syllable_lyrics",
            params=params or None,
        )

    async def get_music_video(
        self, music_video_id: str, *, include: str = "albums"
    ) -> dict[str, Any]:
        """Fetch a catalog music video and its requested relationships."""
        return await self._get(
            f"/v1/catalog/{self.storefront}/music-videos/{music_video_id}",
            operation="get_music_video",
            params={"include": include},
        )

    async def get_uploaded_video(self, uploaded_video_id: str) -> dict[str, Any]:
        """Fetch metadata for an uploaded catalog video."""
        return await self._get(
            f"/v1/catalog/{self.storefront}/uploaded-videos/{uploaded_video_id}",
            operation="get_uploaded_video",
        )

    async def get_album(
        self, album_id: str, *, extend: str = "extendedAssetUrls"
    ) -> dict[str, Any]:
        """Fetch a catalog album with the requested extended asset metadata."""
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
        """Fetch a catalog playlist with up to ``limit_tracks`` embedded tracks.

        For playlists spanning multiple pages, use :meth:`iter_playlist_tracks`
        rather than assuming the embedded track relationship is complete.
        """
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
        """Fetch an artist with selected relationships and views.

        ``include`` and ``views`` are comma-separated Apple Music names.
        ``limit`` applies to each requested relationship or view, not to the
        overall response. For a complete view, use :meth:`iter_artist_view`.
        """
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
        """Fetch one user's library song, optionally including its catalog item."""
        return await self._get(
            f"/v1/me/library/songs/{song_id}",
            operation="get_library_song",
            params={"include": include, "extend": extend},
        )

    async def get_library_music_video(
        self, music_video_id: str, *, include: str = "catalog"
    ) -> dict[str, Any]:
        """Fetch one user's library music video and requested relationships."""
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
        """Fetch one user's library album and requested relationships."""
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
        """Fetch one library playlist and its selected included resources.

        ``limit`` applies to each resource type named in ``include``. For all
        of the playlist's tracks, use :meth:`iter_playlist_tracks` with
        ``is_library=True``.
        """
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
        """Fetch one page of a user-library collection."""
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
        """Fetch one page of library songs; ``limit`` is the page size."""
        return await self._library_list(
            "songs", limit=limit, offset=offset, include=include, extend=extend
        )

    async def get_library_music_videos(
        self, *, limit: int = 100, offset: int = 0, include: str = "catalog"
    ) -> dict[str, Any]:
        """Fetch one page of library music videos; ``offset`` skips prior items."""
        return await self._library_list(
            "music-videos", limit=limit, offset=offset, include=include
        )

    async def get_library_albums(
        self, *, limit: int = 100, offset: int = 0, include: str = "catalog"
    ) -> dict[str, Any]:
        """Fetch one page of library albums without following ``next``."""
        return await self._library_list(
            "albums", limit=limit, offset=offset, include=include
        )

    async def get_library_playlists(
        self, *, limit: int = 100, offset: int = 0, include: str = "catalog"
    ) -> dict[str, Any]:
        """Fetch one page of library playlists without following ``next``."""
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
        """Yield library playlists in server order, across all pages.

        Args:
            limit: Requested number of playlists per page, not a total cap.
            offset: Number of items to skip before the first page.
            include: Comma-separated relationships to request.

        Yields:
            Individual Apple Music library-playlist resource dictionaries.
        """
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
        """Yield library songs lazily, following subsequent pages in order.

        ``limit`` determines page size and ``offset`` the starting position.
        ``include`` and ``extend`` are forwarded to the initial song query.

        Yields:
            Individual library-song resource dictionaries.
        """
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
        """Yield all library music videos, starting at ``offset``.

        ``limit`` is the requested page size; pagination is automatic.
        Each yielded value is a library music-video resource dictionary.
        """
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
        """Yield all library albums in server order, starting at ``offset``.

        ``limit`` specifies the requested page size, not a result limit.
        Each yielded value is a library-album resource dictionary.
        """
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

    async def iter_playlist_tracks(
        self,
        playlist_id: str,
        *,
        is_library: bool = False,
        limit: int = 100,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield playlist tracks in order by following the ``tracks`` endpoint.

        Args:
            playlist_id: ID of the catalog or library playlist.
            is_library: Use the authenticated user's library endpoint instead
                of the storefront's catalog endpoint.
            limit: Requested page size, not a total track limit.

        Yields:
            Individual song or music-video resource dictionaries in playlist
            order, including items beyond the first page.
        """
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
        """Yield an album's tracks in order, following the ``tracks`` cursor.

        Args:
            album_id: Catalog or library album ID.
            is_library: Select the authenticated library album endpoint.
            limit: Requested page size, not a maximum number of tracks.

        Yields:
            Song and music-video resource dictionaries in the order returned
            by Apple Music.
        """
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
        """Yield all resources in one catalog artist view.

        Args:
            artist_id: Catalog artist ID.
            view: Apple Music view name, such as ``full-albums``,
                ``singles``, or ``top-songs``.
            limit: Requested page size, not a total cap.

        Yields:
            Individual view resource dictionaries in server order.

        Raises:
            ValueError: If ``view`` contains path or query separators.
        """
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
        """Retrieve streaming asset metadata for a song or other media item.

        ``kind`` uses Apple's asset API identifiers: ``"song"`` for songs
        and ``"musicVideo"`` for music videos (not ``"music-video"``).
        ``hls_encryption`` and ``hls_profile`` select the requested HLS
        configuration. The response is raw Apple metadata; downloading and
        decrypting media are responsibilities of higher-level components.
        """
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
        """Request playback metadata for a catalog or library track.

        ``is_library`` sends ``universalLibraryId`` instead of the catalog
        ``salableAdamId``. The endpoint may reject unavailable media with a
        ``dialog`` response, which is converted to an API error.
        """
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
        """Exchange a DRM challenge for the track's playback license.

        Args:
            track_id: Apple Music media identifier.
            track_uri: Stream URI associated with the license challenge.
            challenge: Encoded challenge supplied by the DRM component.
            key_system: DRM key-system identifier (defaults to Widevine).
            is_library: Whether the identifier belongs to the user's library.

        Returns:
            Raw license response from Apple Music when ``status`` is zero.

        Notes:
            License requests use POST and are **not** automatically retried.
            Neither the challenge nor license contents should appear in logs.
        """
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
