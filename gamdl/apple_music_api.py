from __future__ import annotations

import functools
import re
import time
import typing
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import urlparse

import requests

from .utils import raise_response_exception


class AppleMusicApi:
    APPLE_MUSIC_HOMEPAGE_URL = "https://music.apple.com"
    AMP_API_URL = "https://amp-api.music.apple.com"
    WEBPLAYBACK_API_URL = (
        "https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/webPlayback"
    )
    LICENSE_API_URL = "https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/acquireWebPlaybackLicense"
    WAIT_TIME = 2

    def __init__(
        self,
        storefront: str,
        media_user_token: str | None = None,
        language: str = "en-US",
    ):
        self.media_user_token = media_user_token
        self.storefront = storefront
        self.language = language
        self._set_session()

    @classmethod
    def from_netscape_cookies(
        cls,
        cookies_path: Path = Path("./cookies.txt"),
        language: str = "en-US",
    ) -> AppleMusicApi:
        parse_cookie = lambda name: next(
            (
                cookie.value
                for cookie in cookies
                if cookie.name == name
                and cookie.domain.endswith(
                    urlparse(cls.APPLE_MUSIC_HOMEPAGE_URL).netloc
                )
            ),
            None,
        )

        cookies = MozillaCookieJar(cookies_path)
        cookies.load(ignore_discard=True, ignore_expires=True)

        media_user_token = parse_cookie("media-user-token")
        if not media_user_token:
            raise ValueError(
                '"media-user-token" cookie not found in cookies. '
                "Make sure you have exported the cookies from Apple Music webpage and are logged in "
                "with an active subscription."
            )

        return cls(
            storefront=None,
            media_user_token=media_user_token,
            language=language,
        )

    def _set_session(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "accept": "*/*",
                "accept-language": "en-US",
                "origin": self.APPLE_MUSIC_HOMEPAGE_URL,
                "priority": "u=1, i",
                "referer": self.APPLE_MUSIC_HOMEPAGE_URL,
                "sec-ch-ua": '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
            }
        )


        token = self._discover_token_from_homepage()


        self.session.headers.update({"authorization": f"Bearer {token}"})
        self.session.params = {"l": self.language}


        if self.media_user_token:
            self.session.cookies.update(
                {
                    "media-user-token": self.media_user_token,
                }
            )
            self._set_account_info()

    def _discover_token_from_homepage(self) -> str:
        """
        Robust token discovery:
        - Try multiple storefront home pages (us/ca).
        - Check inline JWT in HTML.
        - Collect candidates from <script src>, <link rel="modulepreload" href>, and data-src
          covering Vite-style names: index~..., index-legacy~..., and fallback to any /assets/*.js.
        - Prioritize index-legacy~, then index~, then other likely entries.
        - Fetch a handful of candidates and return the first JWT-shaped token found.
        """
        home_urls = [
            f"{self.APPLE_MUSIC_HOMEPAGE_URL}/us/browse",
            f"{self.APPLE_MUSIC_HOMEPAGE_URL}/ca/home",
        ]

        homepage_html = ""
        for u in home_urls:
            try:
                r = self.session.get(
                    u,
                    headers={
                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "accept-language": "en-US,en;q=0.9",
                        "cache-control": "no-cache",
                    },
                    timeout=15,
                )
                if r.ok and r.text:
                    homepage_html = r.text
                    break
            except requests.RequestException:
                continue

        if not homepage_html:
            raise Exception("Failed to load Apple Music homepage for token discovery.")


        jwt_inline = self._find_jwt(homepage_html)
        if jwt_inline:
            return jwt_inline


        candidates = self._collect_asset_candidates(homepage_html)


        for url in candidates[:12]:
            try:
                resp = self.session.get(url, timeout=15)
                if not resp.ok:
                    continue
                token = self._find_jwt(resp.text)
                if token:
                    return token
            except requests.RequestException:
                continue

        raise Exception(
            "Developer token not found in current assets; Apple likely changed bundles. "
            "Update the discovery patterns or supply a server-generated token."
        )

    @staticmethod
    def _find_jwt(text: str) -> str | None:
        """
        JWT-shaped token: header.payload.signature using base64url.
        """
        m = re.search(r'eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+', text)
        return m.group(0) if m else None

    def _collect_asset_candidates(self, html: str) -> list[str]:
        """
        Extract candidate asset URLs from the homepage HTML:
        - Match src/href/data-src attributes pointing to /assets/*.js
        - Prioritize index-legacy~, then index~, then app~/main~/bootstrap~ style entries
        - Normalize to absolute URLs
        """
        urls = set()


        patterns = [
            r'(?:src|href|data-src)=["\'](\/?assets\/index~[a-z0-9]+\.js)["\']',
            r'(?:src|href|data-src)=["\'](\/?assets\/index-legacy~[a-z0-9]+\.js)["\']',
        
            r'(?:src|href|data-src)=["\'](\/?assets\/[A-Za-z0-9/_\-.]+\.js)["\']',
        ]

        for rx in patterns:
            for m in re.finditer(rx, html, flags=re.IGNORECASE):
                urls.add(m.group(1))

        def to_abs(u: str) -> str:
            if u.startswith("http://") or u.startswith("https://"):
                return u
            if u.startswith("/"):
                return f"{self.APPLE_MUSIC_HOMEPAGE_URL}{u}"
            return f"{self.APPLE_MUSIC_HOMEPAGE_URL}/{u}"

        candidates = [to_abs(u) for u in urls]

        def score(s: str) -> int:
            n = s.lower()
            if "index-legacy~" in n:
                return 0
            if "index~" in n:
                return 1
            if any(k in n for k in ("app~", "main~", "bootstrap~")):
                return 2
            return 3

        candidates.sort(key=score)
        return candidates

    def _set_account_info(self):
        self.account_info = self.get_account_info()
        self.storefront = self.account_info["meta"]["subscription"]["storefront"]

    def _check_amp_api_response(self, response: requests.Response) -> None:
        try:
            response.raise_for_status()
            response_dict = response.json()
            assert response_dict.get("data") or response_dict.get("results") is not None
        except (
            requests.HTTPError,
            requests.exceptions.JSONDecodeError,
            AssertionError,
        ):
            raise_response_exception(response)

    def get_account_info(self, meta: str = "subscription") -> dict:
        response = self.session.get(
            f"{self.AMP_API_URL}/v1/me/account",
            params={"meta": meta},
        )
        self._check_amp_api_response(response)

        return response.json()

    def get_artist(
        self,
        artist_id: str,
        include: str = "albums,music-videos",
        limit: int = 100,
        fetch_all: bool = True,
    ) -> dict | None:
        response = self.session.get(
            f"{self.AMP_API_URL}/v1/catalog/{self.storefront}/artists/{artist_id}",
            params={
                "include": include,
                **{f"limit[{_include}]": limit for _include in include.split(",")},
            },
        )
        if response.status_code == 404:
            return None
        self._check_amp_api_response(response)

        artist = response.json()["data"][0]
        if fetch_all:
            for _include in include.split(","):
                for additional_data in self._extend_api_data(
                    artist["relationships"][_include],
                    limit,
                    "",
                ):
                    artist["relationships"][_include]["data"].extend(additional_data)
        return artist

    def get_song(
        self,
        song_id: str,
        extend: str = "extendedAssetUrls",
        include: str = "lyrics,albums",
    ) -> dict | None:
        response = self.session.get(
            f"{self.AMP_API_URL}/v1/catalog/{self.storefront}/songs/{song_id}",
            params={
                "include": include,
                "extend": extend,
            },
        )
        if response.status_code == 404:
            return None
        self._check_amp_api_response(response)

        return response.json()["data"][0]

    def get_music_video(
        self,
        music_video_id: str,
        include: str = "albums",
    ) -> dict | None:
        response = self.session.get(
            f"{self.AMP_API_URL}/v1/catalog/{self.storefront}/music-videos/{music_video_id}",
            params={
                "include": include,
            },
        )
        if response.status_code == 404:
            return None
        self._check_amp_api_response(response)

        return response.json()["data"][0]

    def get_post(
        self,
        post_id: str,
    ) -> dict | None:
        response = self.session.get(
            f"{self.AMP_API_URL}/v1/catalog/{self.storefront}/uploaded-videos/{post_id}"
        )
        if response.status_code == 404:
            return None
        self._check_amp_api_response(response)

        return response.json()["data"][0]

    @functools.lru_cache()
    def get_album(
        self,
        album_id: str,
        extend: str = "extendedAssetUrls",
    ) -> dict | None:
        response = self.session.get(
            f"{self.AMP_API_URL}/v1/catalog/{self.storefront}/albums/{album_id}",
            params={
                "extend": extend,
            },
        )
        if response.status_code == 404:
            return None
        self._check_amp_api_response(response)

        return response.json()["data"][0]

    def get_playlist(
        self,
        playlist_id: str,
        limit_tracks: int = 300,
        extend: str = "extendedAssetUrls",
        fetch_all: bool = True,
    ) -> dict | None:
        response = self.session.get(
            f"{self.AMP_API_URL}/v1/catalog/{self.storefront}/playlists/{playlist_id}",
            params={
                "extend": extend,
                "limit[tracks]": limit_tracks,
            },
        )
        if response.status_code == 404:
            return None
        self._check_amp_api_response(response)

        playlist = response.json()["data"][0]
        if fetch_all:
            for additional_data in self._extend_api_data(
                playlist["relationships"]["tracks"],
                limit_tracks,
                extend,
            ):
                playlist["relationships"]["tracks"]["data"].extend(additional_data)
        return playlist

    def search(
        self,
        term: str,
        types: str = "songs,albums,artists,playlists",
        limit: int = 25,
        offset: int = 0,
    ) -> dict | None:
        response = self.session.get(
            f"{self.AMP_API_URL}/v1/catalog/{self.storefront}/search",
            params={
                "term": term,
                "types": types,
                "limit": limit,
                "offset": offset,
            },
        )
        if response.status_code == 404:
            return None
        self._check_amp_api_response(response)

        return response.json()["results"]

    def get_library_album(
        self,
        album_id: str,
        extend: str = "extendedAssetUrls",
    ) -> dict | None:
        response = self.session.get(
            f"{self.AMP_API_URL}/v1/me/library/albums/{album_id}",
            params={
                "extend": extend,
            },
        )
        if response.status_code == 404:
            return None
        self._check_amp_api_response(response)

        return response.json()["data"][0]

    def get_library_playlist(
        self,
        playlist_id: str,
        include: str = "tracks",
        limit: int = 100,
        extend: str = "extendedAssetUrls",
        fetch_all: bool = True,
    ) -> dict | None:
        response = self.session.get(
            f"{self.AMP_API_URL}/v1/me/library/playlists/{playlist_id}",
            params={
                "include": include,
                **{f"limit[{_include}]": limit for _include in include.split(",")},
                "extend": extend,
            },
        )
        if response.status_code == 404:
            return None
        self._check_amp_api_response(response)

        playlist = response.json()["data"][0]
        if fetch_all:
            for additional_data in self._extend_api_data(
                playlist["relationships"]["tracks"],
                limit,
                extend,
            ):
                playlist["relationships"]["tracks"]["data"].extend(additional_data)
        return playlist

    def _extend_api_data(
        self,
        api_response: dict,
        limit: int,
        extend: str,
    ) -> typing.Generator[list[dict], None, None]:
        next_uri = api_response.get("next")
        while next_uri:
            playlist_next = self._get_next_uri_response(next_uri, limit, extend)
            yield playlist_next["data"]
            next_uri = playlist_next.get("next")
            time.sleep(self.WAIT_TIME)

    def _get_next_uri_response(
        self,
        next_uri: str,
        limit: int,
        extend: str,
    ) -> dict:
        response = self.session.get(
            self.AMP_API_URL + next_uri,
            params={
                "limit": limit,
                "extend": extend,
            },
        )
        self._check_amp_api_response(response)

        return response.json()

    def get_webplayback(
        self,
        track_id: str,
    ) -> dict:
        response = self.session.post(
            self.WEBPLAYBACK_API_URL,
            json={
                "salableAdamId": track_id,
                "language": self.language,
            },
        )

        try:
            response.raise_for_status()
            response_dict = response.json()
            webplayback = response_dict.get("songList")
            assert webplayback
        except (
            requests.HTTPError,
            requests.exceptions.JSONDecodeError,
            AssertionError,
        ):
            raise_response_exception(response)

        return webplayback[0]

    def get_widevine_license(
        self,
        track_id: str,
        track_uri: str,
        challenge: str,
    ) -> str:
        response = self.session.post(
            self.LICENSE_API_URL,
            json={
                "challenge": challenge,
                "key-system": "com.widevine.alpha",
                "uri": track_uri,
                "adamId": track_id,
                "isLibrary": False,
                "user-initiated": True,
            },
        )

        try:
            response.raise_for_status()
            response_dict = response.json()
            widevine_license = response_dict.get("license")
            assert widevine_license
        except (
            requests.HTTPError,
            requests.exceptions.JSONDecodeError,
            AssertionError,
        ):
            raise_response_exception(response)

        return widevine_license
