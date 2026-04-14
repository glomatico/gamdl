import re

import httpx
import structlog

from .constants import (
    APPLE_MUSIC_MUSIC_KIT_URL,
    ITUNES_LOOKUP_API_URL,
    ITUNES_PAGE_API_URL,
)
from .exceptions import GamdlApiResponseError

logger = structlog.get_logger(__name__)


class ItunesApi:
    def __init__(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        self.client = client

    @staticmethod
    async def get_storefront_id(storefront: str) -> int:
        log = logger.bind(action="get_storefront_id", storefront=storefront)

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(APPLE_MUSIC_MUSIC_KIT_URL)
                response.raise_for_status()
                music_kit_content = response.text
            except httpx.HTTPError:
                raise GamdlApiResponseError(
                    "Error fetching MusicKit content",
                    status_code=response.status_code,
                )

        normalized_storefront = storefront.upper()

        country_code_pattern = f'{normalized_storefront}:"([A-Z]{{3}})"'
        country_code_match = re.search(country_code_pattern, music_kit_content)
        if not country_code_match:
            raise GamdlApiResponseError(
                f"Country code {storefront} not found in MusicKit content"
            )

        three_letter_code = country_code_match.group(1)

        storefront_pattern = f'{three_letter_code}:"(\\d+)"'
        storefront_match = re.search(storefront_pattern, music_kit_content)
        if not storefront_match:
            raise GamdlApiResponseError(
                f"Storefront ID not found for country code {storefront}"
            )

        storefront_id = int(storefront_match.group(1))

        log.debug("Extracted storefront", storefront_id=storefront_id)

        return storefront_id

    @classmethod
    async def create(
        cls,
        storefront: str = "us",
        storefront_id: int | None = 143441,
        language: str = "en-US",
    ) -> "ItunesApi":
        storefront_id = storefront_id or await cls.get_storefront_id(storefront)

        client = httpx.AsyncClient(
            params={
                "country": storefront,
                "lang": language,
            },
            headers={
                "X-Apple-Store-Front": f"{storefront_id}-1,32 t:music31",
            },
            timeout=60.0,
        )

        return cls(client=client)

    async def get_lookup_result(
        self,
        media_id: str,
        entity: str = "album",
    ) -> dict:
        log = logger.bind(action="get_lookup_result", media_id=media_id, entity=entity)

        try:
            response = await self.client.get(
                ITUNES_LOOKUP_API_URL,
                params={
                    "id": media_id,
                    "entity": entity,
                },
            )
            response.raise_for_status()
            lookup_result = response.json()
        except httpx.HTTPError:
            raise GamdlApiResponseError(
                "Error fetching iTunes lookup result",
                content=response.text,
                status_code=response.status_code,
            )

        log.debug("Received lookup result", lookup_result=lookup_result)

        return lookup_result

    async def get_itunes_page(
        self,
        media_type: str,
        media_id: str,
    ) -> dict:
        log = logger.bind(
            action="get_itunes_page",
            media_type=media_type,
            media_id=media_id,
        )

        try:
            response = await self.client.get(
                ITUNES_PAGE_API_URL.format(media_type=media_type, media_id=media_id)
            )
            response.raise_for_status()
            itunes_page = response.json()
        except httpx.HTTPError:
            raise GamdlApiResponseError(
                "Error fetching iTunes page",
                content=response.text,
                status_code=response.status_code,
            )

        log.debug("Received iTunes page", itunes_page=itunes_page)

        return itunes_page
