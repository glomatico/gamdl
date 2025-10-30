import asyncio
import base64
import datetime
import logging

from pywidevine import PSSH, Cdm

from ..api.apple_music_api import AppleMusicApi
from ..api.itunes_api import ItunesApi
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
