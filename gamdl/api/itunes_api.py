import logging

import httpx

from ..utils import safe_json
from .constants import ITUNES_LOOKUP_API_URL, ITUNES_PAGE_API_URL, STOREFRONT_IDS
from .exceptions import ApiError

logger = logging.getLogger(__name__)


class ItunesApi:
    def __init__(
        self,
        storefront: str = "us",
        language: str = "en-US",
    ) -> None:
        self.storefront = storefront
        self.language = language
        self.initialize()

    def initialize(self) -> None:
        self._initialize_storefront_id()
        self._initialize_client()

    def _initialize_storefront_id(self) -> None:
        try:
            self.storefront_id = STOREFRONT_IDS[self.storefront.upper()]
        except KeyError:
            raise Exception(f"No storefront id for {self.storefront}")

    def _initialize_client(self) -> None:
        self.client = httpx.AsyncClient(
            params={
                "country": self.storefront,
                "lang": self.language,
            },
            headers={
                "X-Apple-Store-Front": f"{self.storefront_id} t:music31",
            },
            timeout=60.0,
        )

    async def get_lookup_result(
        self,
        media_id: str,
        entity: str = "album",
    ) -> dict:
        response = await self.client.get(
            ITUNES_LOOKUP_API_URL,
            params={
                "id": media_id,
                "entity": entity,
            },
        )
        lookup_result = safe_json(response)

        if response.status_code != 200 or lookup_result is None:
            raise ApiError(
                message=response.text,
                status_code=response.status_code,
            )

        logger.debug(f"Lookup result: {lookup_result}")

        return lookup_result

    async def get_itunes_page(
        self,
        media_type: str,
        media_id: str,
    ) -> dict:
        response = await self.client.get(
            f"{ITUNES_PAGE_API_URL}/{media_type}/{media_id}"
        )
        itunes_page = safe_json(response)

        if response.status_code != 200 or itunes_page is None:
            raise ApiError(
                message=response.text,
                status_code=response.status_code,
            )

        logger.debug(f"iTunes page: {itunes_page}")

        return itunes_page
