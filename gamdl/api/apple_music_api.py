import logging
import re
import typing
from http.cookiejar import MozillaCookieJar
from urllib.parse import parse_qs, urlparse

import httpx

from ..utils import raise_for_status, safe_json
from .constants import (
    AMP_API_URL,
    APPLE_MUSIC_COOKIE_DOMAIN,
    APPLE_MUSIC_HOMEPAGE_URL,
    LICENSE_API_URL,
    WEBPLAYBACK_API_URL,
)

logger = logging.getLogger(__name__)


class AppleMusicApi:
    def __init__(
        self,
        storefront: str = "us",
        media_user_token: str | None = None,
        language: str = "en-US",
    ) -> None:
        self.storefront = storefront
        self.media_user_token = media_user_token
        self.language = language

    @classmethod
    def from_netscape_cookies(
        cls,
        cookies_path: str = "./cookies.txt",
        language: str = "en-US",
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

        return cls(
            storefront=None,
            media_user_token=media_user_token,
            language=language,
        )

    async def setup(self) -> None:
        await self._setup_client()
        await self._setup_token()
        await self._setup_account_info()

    async def _setup_client(self) -> None:
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
            transport=httpx.AsyncHTTPTransport(retries=3),
            timeout=30.0,
        )

    async def _setup_token(self) -> None:
        response = await self.client.get(APPLE_MUSIC_HOMEPAGE_URL)
        raise_for_status(response)
        home_page = response.text

        index_js_uri_match = re.search(
            r"/(assets/index-legacy[~-][^/\"]+\.js)",
            home_page,
        )
        if not index_js_uri_match:
            raise Exception("index.js URI not found in Apple Music homepage")
        index_js_uri = index_js_uri_match.group(1)

        response = await self.client.get(f"{APPLE_MUSIC_HOMEPAGE_URL}/{index_js_uri}")
        raise_for_status(response)
        index_js_page = response.text

        token_match = re.search('(?=eyJh)(.*?)(?=")', index_js_page)
        if not token_match:
            raise Exception("Token not found in index.js page")
        token = token_match.group(1)

        logger.debug(f"Token: {token}")
        self.client.headers.update({"authorization": f"Bearer {token}"})

    async def _setup_account_info(self) -> None:
        if not self.media_user_token:
            return

        self.client.cookies.update(
            {
                "media-user-token": self.media_user_token,
            }
        )

        self.account_info = await self.get_account_info()
        self.storefront = self.account_info["meta"]["subscription"]["storefront"]

    async def get_account_info(self, meta: str | None = "subscription") -> dict:
        response = await self.client.get(
            f"{AMP_API_URL}/v1/me/account",
            params={
                **({"meta": meta} if meta else {}),
            },
        )
        raise_for_status(response)

        account_info = safe_json(response)
        if not "data" in account_info or (meta and "meta" not in account_info):
            raise Exception("Error getting account info:", response.text)
        logger.debug(f"Account info: {account_info}")

        return account_info

    async def get_song(
        self,
        song_id: str,
        extend: str = "extendedAssetUrls",
        include: str = "lyrics,albums",
    ) -> dict | None:
        response = await self.client.get(
            f"{AMP_API_URL}/v1/catalog/{self.storefront}/songs/{song_id}",
            params={
                "extend": extend,
                "include": include,
            },
        )
        raise_for_status(response, {200, 404})

        if response.status_code == 404:
            return None

        song = safe_json(response)
        if not "data" in song:
            raise Exception("Error getting song:", response.text)
        logger.debug(f"Song: {song}")

        return song

    async def get_music_video(
        self,
        music_video_id: str,
        include: str = "albums",
    ) -> dict | None:
        response = await self.client.get(
            f"{AMP_API_URL}/v1/catalog/{self.storefront}/music-videos/{music_video_id}",
            params={
                "include": include,
            },
        )
        raise_for_status(response, {200, 404})

        if response.status_code == 404:
            return None

        music_video = safe_json(response)
        if not "data" in music_video:
            raise Exception("Error getting music video:", response.text)
        logger.debug(f"Music video: {music_video}")

        return music_video

    async def get_uploaded_video(
        self,
        post_id: str,
    ) -> dict | None:
        response = await self.client.get(
            f"{AMP_API_URL}/v1/catalog/{self.storefront}/uploaded-videos/{post_id}"
        )
        raise_for_status(response, {200, 404})

        if response.status_code == 404:
            return None

        uploaded_video = safe_json(response)
        if not "data" in uploaded_video:
            raise Exception("Error getting uploaded video:", response.text)
        logger.debug(f"Uploaded video: {uploaded_video}")

        return uploaded_video

    async def get_album(
        self,
        album_id: str,
        extend: str = "extendedAssetUrls",
    ) -> dict | None:
        response = await self.client.get(
            f"{AMP_API_URL}/v1/catalog/{self.storefront}/albums/{album_id}",
            params={
                "extend": extend,
            },
        )
        raise_for_status(response, {200, 404})

        if response.status_code == 404:
            return None

        album = safe_json(response)
        if not "data" in album:
            raise Exception("Error getting album:", response.text)
        logger.debug(f"Album: {album}")

        return album

    async def get_playlist(
        self,
        playlist_id: str,
        limit_tracks: int = 300,
        extend: str = "extendedAssetUrls",
    ) -> dict | None:
        response = await self.client.get(
            f"{AMP_API_URL}/v1/catalog/{self.storefront}/playlists/{playlist_id}",
            params={
                "limit[tracks]": limit_tracks,
                "extend": extend,
            },
        )
        raise_for_status(response, {200, 404})

        if response.status_code == 404:
            return None

        playlist = safe_json(response)
        if not "data" in playlist:
            raise Exception("Error getting playlist:", response.text)
        logger.debug(f"Playlist: {playlist}")

        return playlist

    async def get_artist(
        self,
        artist_id: str,
        include: str = "albums,music-videos",
        limit: int = 100,
    ) -> dict | None:
        response = await self.client.get(
            f"{AMP_API_URL}/v1/catalog/{self.storefront}/artists/{artist_id}",
            params={
                "include": include,
                **{f"limit[{_include}]": limit for _include in include.split(",")},
            },
        )
        raise_for_status(response, {200, 404})

        if response.status_code == 404:
            return None

        artist = safe_json(response)
        if not "data" in artist:
            raise Exception("Error getting artist:", response.text)
        logger.debug(f"Artist: {artist}")

        return artist

    async def get_library_album(
        self,
        album_id: str,
        extend: str = "extendedAssetUrls",
    ) -> dict | None:
        response = await self.client.get(
            f"{AMP_API_URL}/v1/me/library/albums/{album_id}",
            params={
                "extend": extend,
            },
        )
        raise_for_status(response, {200, 404})

        if response.status_code == 404:
            return None

        album = safe_json(response)
        if not "data" in album:
            raise Exception("Error getting library album:", response.text)
        logger.debug(f"Library album: {album}")

        return album

    async def get_library_playlist(
        self,
        playlist_id: str,
        include: str = "tracks",
        limit: int = 100,
        extend: str = "extendedAssetUrls",
    ) -> dict | None:
        response = await self.client.get(
            f"{AMP_API_URL}/v1/me/library/playlists/{playlist_id}",
            params={
                "include": include,
                **{f"limit[{_include}]": limit for _include in include.split(",")},
                "extend": extend,
            },
        )
        raise_for_status(response, {200, 404})

        if response.status_code == 404:
            return None

        playlist = safe_json(response)
        if not "data" in playlist:
            raise Exception("Error getting library playlist:", response.text)

        return playlist

    async def get_search_results(
        self,
        term: str,
        types: str = "songs,music-videos,albums,playlists,artists",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        response = await self.client.get(
            f"{AMP_API_URL}/v1/catalog/{self.storefront}/search",
            params={
                "term": term,
                "types": types,
                "limit": limit,
                "offset": offset,
            },
        )
        raise_for_status(response)

        search_results = safe_json(response)
        if not "results" in search_results:
            raise Exception("Error searching:", response.text)
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
        response = await self.client.get(
            AMP_API_URL + next_uri,
            params={
                "limit": limit,
                "extend": extend,
                **parse_qs(urlparse(next_uri).query),
            },
        )
        raise_for_status(response)

        extended_api_data = safe_json(response)
        if not "data" in extended_api_data:
            raise Exception("Error getting extended API data:", response.text)
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
        raise_for_status(response)

        webplayback = safe_json(response)
        if not "songList" in webplayback:
            raise Exception("Error getting webplayback:", response.text)
        logger.debug(f"Webplayback: {webplayback}")

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
        raise_for_status(response)

        license_exchange = safe_json(response)
        if not "license" in license_exchange:
            raise Exception("Error getting license exchange:", response.text)
        logger.debug(f"License exchange: {license_exchange}")

        return license_exchange
