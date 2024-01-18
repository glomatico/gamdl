from __future__ import annotations

from requests import Session

from gamdl.constants import *


class ItunesApi:
    def __init__(self, storefront: str = "us", language: str = "en-US"):
        self.storefront = storefront
        self.language = language
        self._setup_session()

    def _setup_session(self):
        self.storefront_id = STOREFRONT_IDS.get(self.storefront.upper())
        if not self.storefront_id:
            raise Exception(f"No storefront id for {self.storefront}")
        self.session = Session()
        self.session.params = {"country": self.storefront, "lang": self.language}
        self.session.headers = {
            "X-Apple-Store-Front": f"{self.storefront_id} t:music31"
        }

    def get_resource(self, params: dict) -> dict:
        response = self.session.get(
            URL_API_LOOKUP,
            params=params,
        )
        if response.status_code != 200:
            raise Exception(f"Failed to get resource:\n{response.text}")
        return response.json()

    def get_resource_itunes_page(self, resource_type: str, resource_id: str) -> dict:
        response = self.session.get(
            f"{URL_API_ITUNES_PAGE}/{resource_type}/{resource_id}"
        )
        if response.status_code != 200:
            raise Exception(f"Failed to get resource iTunes page:\n{response.text}")
        return response.json()
