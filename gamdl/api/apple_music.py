import re
from http.cookiejar import MozillaCookieJar
from urllib.parse import parse_qs, urlparse

import httpx
import structlog
from httpx_retries import Retry, RetryTransport

from .constants import (
    APPLE_MUSIC_ACCOUNT_INFO_API_URI,
    APPLE_MUSIC_ALBUM_API_URI,
    APPLE_MUSIC_AMP_API_URL,
    APPLE_MUSIC_ARTIST_API_URI,
    APPLE_MUSIC_COOKIE_DOMAIN,
    APPLE_MUSIC_HOMEPAGE_URL,
    APPLE_MUSIC_LIBRARY_ALBUM_API_URI,
    APPLE_MUSIC_LIBRARY_PLAYLIST_API_URI,
    APPLE_MUSIC_LICENSE_API_URL,
    APPLE_MUSIC_MUSIC_VIDEO_API_URI,
    APPLE_MUSIC_PLAYLIST_API_URI,
    APPLE_MUSIC_SEARCH_API_URI,
    APPLE_MUSIC_SONG_API_URI,
    APPLE_MUSIC_UPLOADED_VIDEO_API_URL,
    APPLE_MUSIC_WEBPLAYBACK_API_URL,
)
from .exceptions import GamdlApiResponseError

logger = structlog.get_logger(__name__)


class AppleMusicApi:
    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str,
        storefront: str,
        language: str,
        media_user_token: str | None = None,
        account_info: dict | None = None,
    ) -> None:
        self.token = token
        self.storefront = storefront
        self.language = language
        self.media_user_token = media_user_token
        self.account_info = account_info
        self.client = client

    @property
    def active_subscription(self) -> bool:
        if not self.account_info:
            return False

        return (
            self.account_info.get("meta", {})
            .get("subscription", {})
            .get("active", False)
        )

    @property
    def account_restrictions(self) -> dict | None:
        if not self.account_info:
            return None

        data = self.account_info.get("data", [])
        if not data:
            return None
        return data[0].get("attributes", {}).get("restrictions")

    @staticmethod
    async def get_token() -> str:
        log = logger.bind(action="get_token")

        response = None
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    APPLE_MUSIC_HOMEPAGE_URL,
                    follow_redirects=True,
                )
                response.raise_for_status()
                home_page = response.text
            except httpx.HTTPError:
                raise GamdlApiResponseError(
                    "Error fetching Apple Music homepage",
                    status_code=response.status_code if response is not None else None,
                )

        index_js_uri_match = re.search(
            r"/(assets/index-legacy[~-][^/\"]+\.js)",
            home_page,
        )
        if not index_js_uri_match:
            raise GamdlApiResponseError(
                "Error finding index.js URI in Apple Music homepage"
            )
        index_js_uri = index_js_uri_match.group(1)

        response = None
        async with httpx.AsyncClient(follow_redirects=True) as client:
            try:
                response = await client.get(
                    f"{APPLE_MUSIC_HOMEPAGE_URL}/{index_js_uri}"
                )
                response.raise_for_status()
                index_js_page = response.text
            except httpx.HTTPError:
                raise GamdlApiResponseError(
                    "Error fetching index.js page",
                    status_code=response.status_code if response is not None else None,
                )

        token_match = re.search('(?=eyJh)(.*?)(?=")', index_js_page)
        if not token_match:
            raise GamdlApiResponseError("Error finding token in index.js page")
        token = token_match.group(1)

        log.debug("success")

        return token

    @staticmethod
    async def get_account_info(
        token: str,
        media_user_token: str,
        meta: str = "subscription",
    ) -> dict:
        log = logger.bind(action="get_account_info", meta=meta)

        response = None
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    APPLE_MUSIC_AMP_API_URL + APPLE_MUSIC_ACCOUNT_INFO_API_URI,
                    params={
                        "meta": meta,
                    },
                    headers={
                        "authorization": f"Bearer {token}",
                        "origin": APPLE_MUSIC_HOMEPAGE_URL,
                        "cookie": f"media-user-token={media_user_token}",
                    },
                )
                response.raise_for_status()
                account_info = response.json()
            except httpx.HTTPError:
                raise GamdlApiResponseError(
                    "Error fetching account info",
                    status_code=response.status_code if response is not None else None,
                )

        log.debug("success", account_info=account_info)

        return account_info

    @classmethod
    async def create(
        cls,
        storefront: str | None = "us",
        language: str = "en-US",
        token: str | None = None,
        media_user_token: str | None = None,
    ) -> "AppleMusicApi":
        token = token or await cls.get_token()
        account_info = (
            await cls.get_account_info(token, media_user_token)
            if media_user_token
            else None
        )
        storefront = (
            account_info["meta"]["subscription"]["storefront"]
            if account_info
            else storefront
        )
        if not storefront:
            raise ValueError(
                "Storefront must be provided if it cannot be determined from account info"
            )

        client = httpx.AsyncClient(
            headers={
                "authorization": f"Bearer {token}",
                "origin": APPLE_MUSIC_HOMEPAGE_URL,
            },
            transport=RetryTransport(
                retry=Retry(
                    total=6,
                    backoff_factor=1,
                    status_forcelist=[429, 500, 502, 503, 504],
                )
            ),
        )

        if media_user_token:
            client.headers.update(
                {
                    "cookie": f"media-user-token={media_user_token}",
                }
            )

        api = cls(
            client=client,
            token=token,
            storefront=storefront,
            language=language,
            media_user_token=media_user_token,
            account_info=account_info,
        )
        return api

    @classmethod
    async def create_from_netscape_cookies(
        cls,
        cookies_path: str = "./cookies.txt",
        *args,
        **kwargs,
    ) -> "AppleMusicApi":
        cookies = MozillaCookieJar(cookies_path)
        cookies.load(ignore_discard=True, ignore_expires=True)
        parse_cookie = lambda name: next(
            (
                cookie.value
                for cookie in cookies
                if cookie.name == name and cookie.domain == APPLE_MUSIC_COOKIE_DOMAIN
            ),
            None,
        )

        media_user_token = parse_cookie("media-user-token")
        if not media_user_token:
            raise ValueError(
                '"media-user-token" cookie not found in cookies. '
                "Make sure you have exported the cookies from the Apple Music webpage "
                "and are logged in with an active subscription."
            )

        return await cls.create(
            media_user_token=media_user_token,
            *args,
            **kwargs,
        )

    @classmethod
    async def create_from_wrapper(
        cls,
        wrapper_account_url: str = "http://127.0.0.1:30020/",
        *args,
        **kwargs,
    ) -> "AppleMusicApi":
        response = None
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(wrapper_account_url)
                response.raise_for_status()
                wrapper_account_info = response.json()
            except httpx.HTTPError:
                raise GamdlApiResponseError(
                    "Error fetching wrapper account info",
                    status_code=response.status_code if response is not None else None,
                )

        return await cls.create(
            media_user_token=wrapper_account_info["music_token"],
            token=wrapper_account_info["dev_token"],
            *args,
            **kwargs,
        )

    async def _amp_request(
        self,
        uri: str,
        params: dict | None = None,
    ) -> dict:
        response = None
        try:
            response = await self.client.get(
                APPLE_MUSIC_AMP_API_URL + uri,
                params=params,
            )
            response.raise_for_status()
            response_json = response.json()
        except httpx.HTTPError:
            raise GamdlApiResponseError(
                "Error fetching from AMP API",
                content=response.text if response is not None else None,
                status_code=response.status_code if response is not None else None,
            )

        if "errors" in response_json:
            raise GamdlApiResponseError(
                "Error fetching from AMP API",
                content=response_json["errors"],
            )

        return response_json

    async def get_song(
        self,
        song_id: str,
        extend: str = "extendedAssetUrls",
        include: str = "lyrics,albums",
    ) -> dict:
        log = logger.bind(action="get_song", song_id=song_id)

        song = await self._amp_request(
            APPLE_MUSIC_SONG_API_URI.format(
                storefront=self.storefront,
                song_id=song_id,
            ),
            {
                "extend": extend,
                "include": include,
            },
        )

        log.debug("success", song=song)

        return song

    async def get_music_video(
        self,
        music_video_id: str,
        include: str = "albums",
    ) -> dict:
        log = logger.bind(action="get_music_video", music_video_id=music_video_id)

        music_video = await self._amp_request(
            APPLE_MUSIC_MUSIC_VIDEO_API_URI.format(
                storefront=self.storefront,
                music_video_id=music_video_id,
            ),
            {
                "include": include,
            },
        )

        log.debug("success", music_video=music_video)

        return music_video

    async def get_uploaded_video(
        self,
        uploaded_video_id: str,
    ) -> dict:
        log = logger.bind(
            action="get_uploaded_video", uploaded_video_id=uploaded_video_id
        )

        uploaded_video = await self._amp_request(
            APPLE_MUSIC_UPLOADED_VIDEO_API_URL.format(
                storefront=self.storefront,
                uploaded_video_id=uploaded_video_id,
            )
        )

        log.debug("success", uploaded_video=uploaded_video)

        return uploaded_video

    async def get_album(
        self,
        album_id: str,
        extend: str = "extendedAssetUrls",
    ) -> dict:
        log = logger.bind(action="get_album", album_id=album_id)

        album = await self._amp_request(
            APPLE_MUSIC_ALBUM_API_URI.format(
                storefront=self.storefront,
                album_id=album_id,
            ),
            {
                "extend": extend,
            },
        )

        log.debug("success", album=album)

        return album

    async def get_playlist(
        self,
        playlist_id: str,
        limit_tracks: int = 300,
        extend: str = "extendedAssetUrls",
    ) -> dict:
        log = logger.bind(action="get_playlist", playlist_id=playlist_id)

        playlist = await self._amp_request(
            APPLE_MUSIC_PLAYLIST_API_URI.format(
                storefront=self.storefront,
                playlist_id=playlist_id,
            ),
            {
                "limit[tracks]": limit_tracks,
                "extend": extend,
            },
        )

        log.debug("success", playlist=playlist)

        return playlist

    async def get_artist(
        self,
        artist_id: str,
        include: str = "albums,music-videos",
        views: str = "full-albums,compilation-albums,live-albums,singles,top-songs",
        limit: int = 100,
    ) -> dict:
        log = logger.bind(action="get_artist", artist_id=artist_id)

        artist = await self._amp_request(
            APPLE_MUSIC_ARTIST_API_URI.format(
                storefront=self.storefront,
                artist_id=artist_id,
            ),
            {
                "include": include,
                "views": views,
                **{
                    f"limit[{_include}]": limit
                    for _include in [*include.split(","), *views.split(",")]
                },
            },
        )

        log.debug("success", artist=artist)

        return artist

    async def get_library_album(
        self,
        album_id: str,
        extend: str = "extendedAssetUrls",
    ) -> dict:
        log = logger.bind(action="get_library_album", album_id=album_id)

        album = await self._amp_request(
            APPLE_MUSIC_LIBRARY_ALBUM_API_URI.format(
                album_id=album_id,
            ),
            {
                "extend": extend,
            },
        )

        log.debug("success", album=album)

        return album

    async def get_library_playlist(
        self,
        playlist_id: str,
        include: str = "tracks",
        limit: int = 100,
        extend: str = "extendedAssetUrls",
    ) -> dict:
        log = logger.bind(action="get_library_playlist", playlist_id=playlist_id)

        playlist = await self._amp_request(
            APPLE_MUSIC_LIBRARY_PLAYLIST_API_URI.format(
                playlist_id=playlist_id,
            ),
            {
                "include": include,
                **{f"limit[{_include}]": limit for _include in include.split(",")},
                "extend": extend,
            },
        )

        log.debug("success", playlist=playlist)

        return playlist

    async def get_search_results(
        self,
        term: str,
        types: str = "songs,music-videos,albums,playlists,artists",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        log = logger.bind(action="get_search_results", term=term, types=types)

        search_results = await self._amp_request(
            APPLE_MUSIC_SEARCH_API_URI.format(
                storefront=self.storefront,
            ),
            {
                "term": term,
                "types": types,
                "limit": limit,
                "offset": offset,
            },
        )

        log.debug("success", search_results=search_results)

        return search_results

    async def get_extended_api_data(
        self,
        next_uri: str | None,
        href_uri: str,
    ) -> dict:
        log = logger.bind(
            action="extend_api_data", next_uri=next_uri, href_uri=href_uri
        )

        if not next_uri:
            log.debug("no_next_uri")
            return

        href_params = parse_qs(urlparse(href_uri).query)
        next_params = parse_qs(urlparse(next_uri).query)

        if href_params.get("limit"):
            limit = int(href_params["limit"][0])
        else:
            limit = None

        extended_data = await self._amp_request(
            urlparse(next_uri).path,
            {
                **({"limit": limit} if limit else {}),
                **{k: v for k, v in next_params.items() if k not in ["limit"]},
            },
        )

        log.debug("success", extended_data=extended_data)

        return extended_data

    async def get_webplayback(
        self,
        track_id: str,
    ) -> dict:
        log = logger.bind(action="get_webplayback", track_id=track_id)

        response = None
        try:
            response = await self.client.post(
                APPLE_MUSIC_WEBPLAYBACK_API_URL,
                json={
                    "salableAdamId": track_id,
                    "language": self.language,
                },
            )
            response.raise_for_status()
            webplayback = response.json()
        except httpx.HTTPError:
            raise GamdlApiResponseError(
                "Error fetching webplayback data",
                content=response.text if response is not None else None,
                status_code=response.status_code if response is not None else None,
            )

        if "dialog" in webplayback:
            raise GamdlApiResponseError(
                "Error fetching webplayback data",
                content=webplayback["dialog"],
            )

        log.debug("success", webplayback=webplayback)

        return webplayback

    async def get_license_exchange(
        self,
        track_id: str,
        track_uri: str,
        challenge: str,
        key_system: str = "com.widevine.alpha",
        is_library: bool = False,
    ) -> dict:
        log = logger.bind(action="get_license_exchange", track_id=track_id)

        response = None
        try:
            response = await self.client.post(
                APPLE_MUSIC_LICENSE_API_URL,
                json={
                    "challenge": challenge,
                    "key-system": key_system,
                    "uri": track_uri,
                    "adamId": track_id,
                    "isLibrary": is_library,
                    "user-initiated": True,
                },
            )
            response.raise_for_status()
            license_exchange = response.json()
        except httpx.HTTPError:
            raise GamdlApiResponseError(
                "Error fetching license exchange data",
                content=response.text if response is not None else None,
                status_code=response.status_code if response is not None else None,
            )

        if license_exchange.get("status") != 0:
            raise GamdlApiResponseError(
                "Error fetching license exchange data",
                content=response.text,
                status_code=response.status_code,
            )

        log.debug("success", license_exchange=license_exchange)

        return license_exchange
