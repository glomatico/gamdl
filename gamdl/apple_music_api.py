from __future__ import annotations

import functools
import re
import time
import typing
from http.cookiejar import MozillaCookieJar
from pathlib import Path

import requests


class AppleMusicApi:
    APPLE_MUSIC_HOMEPAGE_URL = "https://beta.music.apple.com"
    AMP_API_URL = "https://amp-api.music.apple.com"
    WEBPLAYBACK_API_URL = (
        "https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/webPlayback"
    )
    LICENSE_API_URL = "https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/acquireWebPlaybackLicense"
    WAIT_TIME = 2

    def __init__(
        self,
        cookies_path: Path | None = Path("./cookies.txt"),
        storefront: None | str = None,
        language: str = "en-US",
    ):
        self.cookies_path = cookies_path
        self.storefront = storefront
        self.language = language
        self._set_session()

    def _set_session(self):
        self.session = requests.Session()
        if self.cookies_path:
            cookies = MozillaCookieJar(self.cookies_path)
            cookies.load(ignore_discard=True, ignore_expires=True)
            self.session.cookies.update(cookies)
            self.storefront = self.session.cookies.get_dict()["itua"]
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:95.0) Gecko/20100101 Firefox/95.0",
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate, br",
                "content-type": "application/json",
                "Media-User-Token": self.session.cookies.get_dict().get(
                    "media-user-token", ""
                ),
                "x-apple-renewal": "true",
                "DNT": "1",
                "Connection": "keep-alive",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
                "origin": self.APPLE_MUSIC_HOMEPAGE_URL,
            }
        )
        home_page = self.session.get(self.APPLE_MUSIC_HOMEPAGE_URL).text
        index_js_uri = re.search(
            r"/(assets/index-legacy-[^/]+\.js)",
            home_page,
        ).group(1)
        index_js_page = self.session.get(
            f"{self.APPLE_MUSIC_HOMEPAGE_URL}/{index_js_uri}"
        ).text
        token = re.search('(?=eyJh)(.*?)(?=")', index_js_page).group(1)
        self.session.headers.update({"authorization": f"Bearer {token}"})
        self.session.params = {"l": self.language}

    @staticmethod
    def _raise_response_exception(response: requests.Response):
        raise Exception(
            f"Request failed with status code {response.status_code}: {response.text}"
        )

    def _check_amp_api_response(self, response: requests.Response):
        try:
            response.raise_for_status()
            response_dict = response.json()
            assert response_dict.get("data") or response_dict.get("results") is not None
        except (
            requests.HTTPError,
            requests.exceptions.JSONDecodeError,
            AssertionError,
        ):
            self._raise_response_exception(response)

    def get_artist(
        self,
        artist_id: str,
        include: str = "albums,music-videos",
        limit: int = 100,
        fetch_all: bool = True,
    ) -> dict:
        response = self.session.get(
            f"{self.AMP_API_URL}/v1/catalog/{self.storefront}/artists/{artist_id}",
            params={
                "include": include,
                **{f"limit[{_include}]": limit for _include in include.split(",")},
            },
        )
        self._check_amp_api_response(response)
        artist = response.json()["data"][0]
        if fetch_all:
            for _include in include.split(","):
                for additional_data in self._extend_api_data(
                    artist["relationships"][_include],
                    limit,
                ):
                    artist["relationships"][_include]["data"].extend(additional_data)
        return artist

    def get_song(
        self,
        song_id: str,
        extend: str = "extendedAssetUrls",
        include: str = "lyrics,albums",
    ) -> dict:
        response = self.session.get(
            f"{self.AMP_API_URL}/v1/catalog/{self.storefront}/songs/{song_id}",
            params={
                "include": include,
                "extend": extend,
            },
        )
        self._check_amp_api_response(response)
        return response.json()["data"][0]

    def get_music_video(
        self,
        music_video_id: str,
        include: str = "albums",
    ) -> dict:
        response = self.session.get(
            f"{self.AMP_API_URL}/v1/catalog/{self.storefront}/music-videos/{music_video_id}",
            params={
                "include": include,
            },
        )
        self._check_amp_api_response(response)
        return response.json()["data"][0]

    def get_post(
        self,
        post_id: str,
    ) -> dict:
        response = self.session.get(
            f"{self.AMP_API_URL}/v1/catalog/{self.storefront}/uploaded-videos/{post_id}"
        )
        self._check_amp_api_response(response)
        return response.json()["data"][0]

    @functools.lru_cache()
    def get_album(
        self,
        album_id: str,
        extend: str = "extendedAssetUrls",
    ) -> dict:
        response = self.session.get(
            f"{self.AMP_API_URL}/v1/catalog/{self.storefront}/albums/{album_id}",
            params={
                "extend": extend,
            },
        )
        self._check_amp_api_response(response)
        return response.json()["data"][0]

    def get_playlist(
        self,
        playlist_id: str,
        limit_tracks: int = 300,
        extend: str = "extendedAssetUrls",
        fetch_all: bool = True,
    ) -> dict:
        response = self.session.get(
            f"{self.AMP_API_URL}/v1/catalog/{self.storefront}/playlists/{playlist_id}",
            params={
                "extend": extend,
                "limit[tracks]": limit_tracks,
            },
        )
        self._check_amp_api_response(response)
        playlist = response.json()["data"][0]
        if fetch_all:
            for additional_data in self._extend_api_data(
                playlist["relationships"]["tracks"],
                limit_tracks,
            ):
                playlist["relationships"]["tracks"]["data"].extend(additional_data)
        return playlist

    def search(
        self,
        term: str,
        types: str = "songs,albums,artists,playlists",
        limit: int = 25,
        offset: int = 0,
    ) -> dict:

        response = self.session.get(
            f"{self.AMP_API_URL}/v1/catalog/{self.storefront}/search",
            params={
                "term": term,
                "types": types,
                "limit": limit,
                "offset": offset,
            },
        )
        self._check_amp_api_response(response)
        return response.json()["results"]

    def _extend_api_data(
        self,
        api_response: dict,
        limit: int,
    ) -> typing.Generator[list[dict], None, None]:
        next_uri = api_response.get("next")
        while next_uri:
            playlist_next = self._get_next_uri_response(next_uri, limit)
            yield playlist_next["data"]
            next_uri = playlist_next.get("next")
            time.sleep(self.WAIT_TIME)

    def _get_next_uri_response(self, next_uri: str, limit: int) -> dict:
        response = self.session.get(
            self.AMP_API_URL + next_uri,
            params={
                "limit": limit,
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
            self._raise_response_exception(response)
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
            self._raise_response_exception(response)
        return widevine_license
