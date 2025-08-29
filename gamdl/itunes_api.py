from __future__ import annotations

import functools

import requests

from .constants import STOREFRONT_IDS
from .utils import raise_response_exception


class ItunesApi:
    ITUNES_LOOKUP_API_URL = "https://itunes.apple.com/lookup"
    ITUNES_PAGE_API_URL = "https://music.apple.com"

    def __init__(
        self,
        storefront: str = "us",
        language: str = "en-US",
    ):
        self.storefront = storefront
        self.language = language
        self._setup_session()

    def _setup_session(self):
        try:
            self.storefront_id = STOREFRONT_IDS[self.storefront.upper()]
        except KeyError:
            raise Exception(f"No storefront id for {self.storefront}")
        self.session = requests.Session()
        self.session.params = {
            "country": self.storefront,
            "lang": self.language,
        }
        self.session.headers = {
            "X-Apple-Store-Front": f"{self.storefront_id} t:music31",
        }

    @functools.lru_cache()
    def get_resource(
        self,
        resource_id: str,
        entity: str = "album",
    ) -> dict | None:
        response = self.session.get(
            self.ITUNES_LOOKUP_API_URL,
            params={
                "id": resource_id,
                "entity": entity,
            },
        )
        try:
            response.raise_for_status()
            response_dict = response.json()
        except (
            requests.HTTPError,
            requests.exceptions.JSONDecodeError,
        ):
            raise_response_exception(response)
        if response_dict.get("results"):
            return response_dict["results"]
        return None

    def get_itunes_page(
        self,
        resource_type: str,
        resource_id: str,
    ) -> dict | None:
        response = self.session.get(
            f"{self.ITUNES_PAGE_API_URL}/{resource_type}/{resource_id}"
        )
        try:
            response.raise_for_status()
            response_dict = response.json()
            itunes_page = response_dict["storePlatformData"]["product-dv"][
                "results"
            ].get(resource_id)
        except (
            requests.HTTPError,
            requests.exceptions.JSONDecodeError,
        ):
            raise_response_exception(response)
        return itunes_page
