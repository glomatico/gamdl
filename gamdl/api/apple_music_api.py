import logging
import re
import typing
from http.cookiejar import MozillaCookieJar
from urllib.parse import parse_qs, urlparse

import httpx

from ..utils import get_response, raise_for_status, safe_json
from .constants import (
    AMP_API_URL,
    APPLE_MUSIC_COOKIE_DOMAIN,
    APPLE_MUSIC_HOMEPAGE_URL,
    LICENSE_API_URL,
    WEBPLAYBACK_API_URL,
)
from .exceptions import ApiError

logger = logging.getLogger(__name__)


class AppleMusicApi:
    def __init__(
        self,
        storefront: str = "us",
        language: str = "en-US",
        media_user_token: str | None = None,
        developer_token: str | None = None,
    ) -> None:
        self.storefront = storefront
        self.language = language
        self.media_user_token = media_user_token
        self.token = developer_token

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
            storefront=None,
            media_user_token=media_user_token,
            developer_token=None,
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
        wrapper_account_response = await get_response(wrapper_account_url)
        wrapper_account_info = safe_json(wrapper_account_response)

        return await cls.create(
            storefront=None,
            media_user_token=wrapper_account_info["music_token"],
            developer_token=wrapper_account_info["dev_token"],
            *args,
            **kwargs,
        )

    @classmethod
    async def create(
        cls,
        storefront: str | None = "us",
        language: str = "en-US",
        media_user_token: str | None = None,
        developer_token: str | None = None,
    ) -> "AppleMusicApi":
        api = cls(
            storefront=storefront,
            language=language,
            media_user_token=media_user_token,
            developer_token=developer_token,
        )
        await api.initialize()
        return api

    async def initialize(self) -> None:
        await self._initialize_client()
        await self._initialize_token()
        await self._initialize_account_info()

    async def _initialize_client(self) -> None:
        self.client = httpx.AsyncClient(
            headers={
                "accept": "*/*",
                "accept-language": "en-US",
                "origin": APPLE_MUSIC_HOMEPAGE_URL,
                "priority": "u=1, i",
                "referer": APPLE_MUSIC_HOMEPAGE_URL,
                "sec-ch-ua": '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
            },
            params={
                "l": self.language,
            },
            follow_redirects=True,
            timeout=60.0,
        )

    async def _get_token(self) -> str:
        response = await self.client.get(APPLE_MUSIC_HOMEPAGE_URL)
        home_page = response.text

        index_js_uri_match = re.search(
            r"/(assets/index-legacy[~-][^/\"]+\.js)",
            home_page,
        )
        if not index_js_uri_match:
            raise Exception("index.js URI not found in Apple Music homepage")
        index_js_uri = index_js_uri_match.group(1)

        response = await self.client.get(f"{APPLE_MUSIC_HOMEPAGE_URL}/{index_js_uri}")
        index_js_page = response.text

        token_match = re.search('(?=eyJh)(.*?)(?=")', index_js_page)
        if not token_match:
            raise Exception("Token not found in index.js page")
        token = token_match.group(1)

        logger.debug(f"Token: {token}")
        return token

    async def _initialize_token(self) -> None:
        self.token = self.token or await self._get_token()
        self.client.headers.update({"authorization": f"Bearer {self.token}"})

    async def _initialize_account_info(self) -> None:
        if not self.media_user_token:
            return

        self.client.cookies.update(
            {
                "media-user-token": self.media_user_token,
            }
        )

        self.account_info = await self.get_account_info()
        self.storefront = self.account_info["meta"]["subscription"]["storefront"]

    @property
    def active_subscription(self) -> bool:
        return (
            getattr(self, "account_info", {})
            .get("meta", {})
            .get("subscription", {})
            .get("active", False)
        )

    @property
    def account_restrictions(self) -> dict | None:
        data = getattr(self, "account_info", {}).get("data", [])
        if not data:
            return None
        return data[0].get("attributes", {}).get("restrictions")

    async def get_account_info(self, meta: str = "subscription") -> dict:
        account_info = await self._amp_request(
            f"/v1/me/account",
            {
                "meta": meta,
            },
        )
        logger.debug(f"Account info: {account_info}")

        return account_info

    async def _amp_request(
        self,
        endpoint: str,
        params: dict | None = None,
    ) -> dict:
        response = await self.client.get(
            AMP_API_URL + endpoint,
            params=params or {},
        )
        response_json = safe_json(response)

        if (
            response.status_code != 200
            or response_json is None
            or "errors" in response_json
        ):
            raise ApiError(
                message=response.text,
                status_code=response.status_code,
            )

        return response_json

    async def get_song(
        self,
        song_id: str,
        extend: str = "extendedAssetUrls",
        include: str = "lyrics,albums",
    ) -> dict | None:
        song = await self._amp_request(
            f"/v1/catalog/{self.storefront}/songs/{song_id}",
            {
                "extend": extend,
                "include": include,
            },
        )
        logger.debug(f"Song: {song}")

        return song

    async def get_music_video(
        self,
        music_video_id: str,
        include: str = "albums",
    ) -> dict | None:
        music_video = await self._amp_request(
            f"/v1/catalog/{self.storefront}/music-videos/{music_video_id}",
            {
                "include": include,
            },
        )
        logger.debug(f"Music video: {music_video}")

        return music_video

    async def get_uploaded_video(
        self,
        post_id: str,
    ) -> dict | None:
        uploaded_video = await self._amp_request(
            f"/v1/catalog/{self.storefront}/uploaded-videos/{post_id}",
        )
        logger.debug(f"Uploaded video: {uploaded_video}")

        return uploaded_video

    async def get_album(
        self,
        album_id: str,
        extend: str = "extendedAssetUrls",
    ) -> dict | None:
        album = await self._amp_request(
            f"/v1/catalog/{self.storefront}/albums/{album_id}",
            {
                "extend": extend,
            },
        )
        logger.debug(f"Album: {album}")

        return album

    async def get_playlist(
        self,
        playlist_id: str,
        limit_tracks: int = 300,
        extend: str = "extendedAssetUrls",
    ) -> dict | None:
        playlist = await self._amp_request(
            f"/v1/catalog/{self.storefront}/playlists/{playlist_id}",
            {
                "limit[tracks]": limit_tracks,
                "extend": extend,
            },
        )
        logger.debug(f"Playlist: {playlist}")

        return playlist

    async def get_artist(
        self,
        artist_id: str,
        include: str = "albums,music-videos",
        views: str = "full-albums,compilation-albums,live-albums,singles,top-songs",
        limit: int = 100,
    ) -> dict | None:
        artist = await self._amp_request(
            f"/v1/catalog/{self.storefront}/artists/{artist_id}",
            {
                "include": include,
                "views": views,
                **{
                    f"limit[{_include}]": limit
                    for _include in [*include.split(","), *views.split(",")]
                },
            },
        )
        logger.debug(f"Artist: {artist}")

        return artist

    async def get_library_album(
        self,
        album_id: str,
        extend: str = "extendedAssetUrls",
    ) -> dict | None:
        album = await self._amp_request(
            f"/v1/me/library/albums/{album_id}",
            {
                "extend": extend,
            },
        )
        logger.debug(f"Library album: {album}")

        return album

    async def get_library_playlist(
        self,
        playlist_id: str,
        include: str = "tracks",
        limit: int = 100,
        extend: str = "extendedAssetUrls",
    ) -> dict | None:
        playlist = await self._amp_request(
            f"/v1/me/library/playlists/{playlist_id}",
            {
                "include": include,
                **{f"limit[{_include}]": limit for _include in include.split(",")},
                "extend": extend,
            },
        )
        logger.debug(f"Library playlist: {playlist}")

        return playlist

    async def get_search_results(
        self,
        term: str,
        types: str = "songs,music-videos,albums,playlists,artists",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        search_results = await self._amp_request(
            f"/v1/catalog/{self.storefront}/search",
            {
                "term": term,
                "types": types,
                "limit": limit,
                "offset": offset,
            },
        )
        logger.debug(f"Search results: {search_results}")

        return search_results

    async def extend_api_data(
        self,
        api_response: dict,
        extend: str = "extendedAssetUrls",
    ) -> typing.AsyncGenerator[dict, None]:
        next_uri = api_response.get("next")
        if not next_uri:
            return

        next_uri_params = parse_qs(urlparse(next_uri).query)
        limit = int(next_uri_params["offset"][0])
        while next_uri:
            extended_api_data = await self._get_extended_api_data(
                next_uri,
                limit,
                extend,
            )
            yield extended_api_data
            next_uri = extended_api_data.get("next")

    async def _get_extended_api_data(
        self,
        next_uri: str,
        limit: int,
        extend: str,
    ) -> dict:
        next_uri_params = parse_qs(urlparse(next_uri).query)
        params = {
            "limit": limit,
            "offset": next_uri_params["offset"][0],
            "extend": extend,
        }
        extended_api_data = await self._amp_request(next_uri, params)
        logger.debug(f"Extended API data: {extended_api_data}")

        return extended_api_data

    async def get_webplayback(
        self,
        track_id: str,
    ) -> dict:
        response = await self.client.post(
            WEBPLAYBACK_API_URL,
            json={
                "salableAdamId": track_id,
                "language": self.language,
            },
        )
        webplayback = safe_json(response)

        if (
            response.status_code != 200
            or webplayback is None
            or "dialog" in webplayback
        ):
            raise ApiError(
                message=response.text,
                status_code=response.status_code,
            )

        return webplayback

    async def get_license_exchange(
        self,
        track_id: str,
        track_uri: str,
        challenge: str,
        key_system: str = "com.widevine.alpha",
    ) -> dict:
        response = await self.client.post(
            LICENSE_API_URL,
            json={
                "challenge": challenge,
                "key-system": key_system,
                "uri": track_uri,
                "adamId": track_id,
                "isLibrary": False,
                "user-initiated": True,
            },
        )
        license_exchange = safe_json(response)

        if (
            response.status_code != 200
            or license_exchange is None
            or license_exchange.get("status") != 0
        ):
            raise ApiError(
                message=response.text,
                status_code=response.status_code,
            )

        logger.debug(f"License exchange: {license_exchange}")

        return license_exchange
