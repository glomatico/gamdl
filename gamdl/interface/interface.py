import asyncio
import base64
import datetime
import logging
import re
from io import BytesIO

from async_lru import alru_cache
from PIL import Image
from pywidevine import PSSH, Cdm

from ..api.apple_music_api import AppleMusicApi
from ..api.itunes_api import ItunesApi
from ..utils import get_response
from .constants import IMAGE_FILE_EXTENSION_MAP
from .enums import CoverFormat
from .types import DecryptionKey

logger = logging.getLogger(__name__)


class AppleMusicInterface:
    def __init__(
        self,
        apple_music_api: AppleMusicApi,
        itunes_api: ItunesApi,
    ) -> None:
        self.apple_music_api = apple_music_api
        self.itunes_api = itunes_api

    @staticmethod
    def get_media_id_of_library_media(library_media_metadata: dict) -> str:
        play_params = library_media_metadata["attributes"].get("playParams", {})
        return play_params.get("catalogId", library_media_metadata["id"])

    @staticmethod
    def parse_date(date: str) -> datetime.datetime:
        return datetime.datetime.fromisoformat(date.split("Z")[0])

    async def get_decryption_key(
        self,
        track_uri: str,
        track_id: str,
        cdm: Cdm,
    ) -> DecryptionKey:
        try:
            cdm_session = cdm.open()

            pssh_obj = PSSH(track_uri.split(",")[-1])

            challenge = base64.b64encode(
                await asyncio.to_thread(
                    cdm.get_license_challenge, cdm_session, pssh_obj
                )
            ).decode()
            license = await self.apple_music_api.get_license_exchange(
                track_id,
                track_uri,
                challenge,
            )

            await asyncio.to_thread(cdm.parse_license, cdm_session, license["license"])
            decryption_key_info = next(
                i for i in cdm.get_keys(cdm_session) if i.type == "CONTENT"
            )
        finally:
            cdm.close(cdm_session)

        decryption_key = DecryptionKey(
            key=decryption_key_info.key.hex(),
            kid=decryption_key_info.kid.hex,
        )
        logger.debug(f"Decryption key: {decryption_key}")

        return decryption_key

    def get_cover_url_template(self, metadata: dict, cover_format: CoverFormat) -> str:
        if cover_format == CoverFormat.RAW:
            cover_url_template = self._get_raw_cover_url(
                metadata["attributes"]["artwork"]["url"]
            )
        cover_url_template = metadata["attributes"]["artwork"]["url"]

        logger.debug(f"Cover URL template: {cover_url_template}")
        return cover_url_template

    def _get_raw_cover_url(self, cover_url_template: str) -> str:
        return re.sub(
            r"image/thumb/",
            "",
            re.sub(
                r"is1-ssl",
                "a1",
                cover_url_template,
            ),
        )

    def get_cover_url(
        self,
        cover_url_template: str,
        cover_size: int,
        cover_format: CoverFormat,
    ) -> str:
        cover_url = re.sub(
            r"\{w\}x\{h\}([a-z]{2})\.jpg",
            (
                f"{cover_size}x{cover_size}bb.{cover_format.value}"
                if cover_format != CoverFormat.RAW
                else ""
            ),
            cover_url_template,
        )

        logger.debug(f"Cover URL: {cover_url}")
        return cover_url

    @alru_cache()
    async def get_cover_file_extension(
        self,
        cover_url: str,
        cover_format: CoverFormat,
    ) -> str | None:
        if cover_format != CoverFormat.RAW:
            return f".{cover_format.value}"

        cover_url = self.get_cover_url(cover_url)
        cover_bytes = await self.get_cover_bytes(cover_url)
        if cover_bytes is None:
            return None

        image_obj = Image.open(BytesIO(self.get_cover_bytes(cover_url)))
        image_format = image_obj.format.lower()
        return IMAGE_FILE_EXTENSION_MAP.get(
            image_format,
            f".{image_format.lower()}",
        )

    @alru_cache()
    async def get_cover_bytes(self, cover_url: str) -> bytes | None:
        response = await get_response(cover_url, {200, 404})
        if response.status_code == 200:
            return response.content
        return None

    @alru_cache()
    async def get_media_date(
        self,
        media_id: str,
    ) -> datetime.datetime | None:
        lookup_result = await self.itunes_api.get_lookup_result(media_id)
        if not lookup_result["results"]:
            return None

        release_date = lookup_result["results"][0].get("releaseDate")
        if not release_date:
            return None

        parsed_date = self.parse_date(release_date)
        logger.debug(f"Parsed media date: {parsed_date}")

        return parsed_date
