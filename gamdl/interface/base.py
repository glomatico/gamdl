import asyncio
import base64
import datetime
import re
from io import BytesIO

import httpx
import structlog
from async_lru import alru_cache
from PIL import Image
from pywidevine import PSSH, Cdm, Device
from pywidevine.license_protocol_pb2 import WidevinePsshData

from gamdl.interface.wvd import WVD

from ..api.apple_music import AppleMusicApi
from ..api.itunes import ItunesApi
from ..api.wrapper import WrapperApi
from .constants import IMAGE_FILE_EXTENSION_MAP
from .enums import CoverFormat
from .types import Cover, DecryptionKey, MediaRating, MediaTags, MediaType, PlaylistTags

logger = structlog.get_logger(__name__)


class AppleMusicBaseInterface:
    def __init__(
        self,
        apple_music_api: AppleMusicApi,
        itunes_api: ItunesApi,
        wrapper_api: WrapperApi | None,
        cover_format: CoverFormat,
        cover_size: int,
        cdm: Cdm,
    ) -> None:
        self.apple_music_api = apple_music_api
        self.itunes_api = itunes_api
        self.cover_format = cover_format
        self.cover_size = cover_size
        self.cdm = cdm
        self.wrapper_api = wrapper_api

    @staticmethod
    def create_cdm(wvd_path: str | None = None) -> Cdm:
        if wvd_path:
            cdm = Cdm.from_device(Device.load(wvd_path))
        else:
            cdm = Cdm.from_device(Device.loads(WVD))
        cdm.MAX_NUM_OF_SESSIONS = float("inf")

        return cdm

    @staticmethod
    def is_media_streamable(
        media_metadata: dict,
    ) -> bool:
        return bool(media_metadata["attributes"].get("playParams"))

    @staticmethod
    def parse_media_id_from_url(media_metadata: dict) -> str | None:
        media_url = media_metadata["attributes"].get("url")
        if media_url is None:
            return None

        url_media_id = media_url.split("/")[-1].split("?")[0]

        return url_media_id

    @staticmethod
    def parse_date(date: str) -> datetime.datetime:
        return datetime.datetime.fromisoformat(date.split("Z")[0])

    @staticmethod
    def reconstruct_pssh(pssh: str) -> bytes:
        pssh = pssh.split(",")[-1]

        decoded_pssh = base64.b64decode(pssh)
        if len(decoded_pssh) > 30:
            return pssh

        widevine_pssh_data = WidevinePsshData(
            algorithm=1,
            key_ids=[decoded_pssh],
        )

        return widevine_pssh_data.SerializeToString()

    @staticmethod
    async def get_response(
        url: str,
        valid_responses: list[int] = [200],
    ) -> httpx.Response:
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                if e.response.status_code in valid_responses:
                    return e.response
                raise e

        return response

    @staticmethod
    def format_cover(
        template_cover_url: str,
        cover_size: int,
        cover_format: CoverFormat,
    ) -> str:
        return re.sub(
            r"/\{w\}x\{h\}([a-z]{2})\.jpg",
            f"/{cover_size}x{cover_size}bb.{cover_format.value}",
            template_cover_url,
        )

    @staticmethod
    def get_catalog_metadata_from_library(library_metadata: dict) -> dict | None:
        data = library_metadata.get("relationships", {}).get("catalog", {}).get("data")
        if not data:
            return None

        return data[0]

    @classmethod
    async def create(
        cls,
        apple_music_api: AppleMusicApi,
        cover_format: CoverFormat = CoverFormat.JPG,
        cover_size: int = 1200,
        wvd_path: str | None = None,
        itunes_api: ItunesApi | None = None,
        wrapper_api: WrapperApi | None = None,
    ):
        itunes_api = itunes_api or await ItunesApi.create(
            storefront=apple_music_api.storefront,
            language=apple_music_api.language,
            **(
                {"storefront_id": None}
                if apple_music_api.storefront.lower() != "us"
                else {}
            ),
        )
        cdm = cls.create_cdm(wvd_path)

        base = cls(
            apple_music_api=apple_music_api,
            itunes_api=itunes_api,
            cover_format=cover_format,
            cover_size=cover_size,
            cdm=cdm,
            wrapper_api=wrapper_api,
        )
        return base

    @alru_cache()
    async def get_album_cached(
        self,
        album_id: int,
    ) -> dict | None:
        return (await self.apple_music_api.get_album(album_id))["data"][0]

    async def get_decryption_key(
        self,
        pssh: str,
        track_id: str,
    ) -> DecryptionKey:
        log = logger.bind(action="get_decryption_key", track_id=track_id)

        reconstructed_pssh = self.reconstruct_pssh(pssh)
        cdm_session = self.cdm.open()

        try:
            pssh_obj = PSSH(reconstructed_pssh)

            challenge = base64.b64encode(
                await asyncio.to_thread(
                    self.cdm.get_license_challenge, cdm_session, pssh_obj
                )
            ).decode()
            license = await self.apple_music_api.get_license_exchange(
                track_id,
                pssh,
                challenge,
            )

            await asyncio.to_thread(
                self.cdm.parse_license, cdm_session, license["license"]
            )
            decryption_key_info = next(
                i for i in self.cdm.get_keys(cdm_session) if i.type == "CONTENT"
            )
        finally:
            self.cdm.close(cdm_session)

        decryption_key = DecryptionKey(
            key=decryption_key_info.key.hex(),
            kid=decryption_key_info.kid.hex,
        )

        log.debug("success", decryption_key=decryption_key)

        return decryption_key

    @alru_cache()
    async def get_cover_bytes(self, cover_url: str) -> bytes | None:
        log = logger.bind(action="get_cover_bytes", cover_url=cover_url)

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(cover_url, follow_redirects=True)

            if response.status_code == 404:
                log.debug("cover_not_found")
                return None

            response.raise_for_status()

            return response.content

    def _get_cover_template_url(self, metadata: dict) -> str:
        if self.cover_format == CoverFormat.RAW:
            cover_template_url = self._get_raw_cover_url(
                metadata["attributes"]["artwork"]["url"]
            )
        else:
            cover_template_url = metadata["attributes"]["artwork"]["url"]

        return cover_template_url

    def _get_raw_cover_url(self, cover_url_template: str) -> str:
        return re.sub(
            r"/\{w\}x\{h\}(?:bb|mv)\.jpg",
            "",
            re.sub(
                r"image/thumb/",
                "",
                re.sub(
                    r"is1-ssl",
                    "a1",
                    cover_url_template,
                ),
            ),
        )

    @alru_cache()
    async def _get_cover_file_extension(
        self,
        cover_url: str,
    ) -> str | None:
        log = logger.bind(action="get_cover_file_extension", cover_url=cover_url)
        if self.cover_format != CoverFormat.RAW:
            return f".{self.cover_format.value}"

        cover_bytes = await self.get_cover_bytes(cover_url)
        if cover_bytes is None:
            log.debug("cover_bytes_empty")
            return None

        image_obj = Image.open(BytesIO(cover_bytes))
        image_format = image_obj.format.lower()
        return IMAGE_FILE_EXTENSION_MAP.get(
            image_format,
            f".{image_format.lower()}",
        )

    async def get_cover(
        self,
        metadata: dict,
    ) -> str:
        log = logger.bind(action="get_cover", media_id=metadata["id"])

        template_url = self._get_cover_template_url(metadata)

        if self.cover_format == CoverFormat.RAW:
            cover_url = template_url
        else:
            cover_url = self.format_cover(
                template_url,
                self.cover_size,
                self.cover_format,
            )

        cover_file_extension = await self._get_cover_file_extension(cover_url)

        cover = Cover(
            template_url=template_url,
            url=cover_url,
            file_extension=cover_file_extension,
        )

        log.debug("success", cover=cover)

        return cover

    @alru_cache()
    async def get_media_date(
        self,
        media_id: str,
    ) -> datetime.datetime | None:
        log = logger.bind(action="get_media_date", media_id=media_id)

        lookup_result = await self.itunes_api.get_lookup_result(media_id)
        if not lookup_result["results"]:
            log.debug("no_media_id")
            return None

        release_date = lookup_result["results"][0].get("releaseDate")
        if not release_date:
            log.debug("no_release_date")
            return None

        parsed_date = self.parse_date(release_date)

        log.debug("success", release_date=parsed_date)

        return parsed_date

    def get_playlist_tags(
        self,
        playlist_metadata: dict,
        playlist_track: int,
    ) -> PlaylistTags:
        log = logger.bind(
            action="get_playlist_tags",
            playlist_id=playlist_metadata["id"],
        )

        playlist_tags = PlaylistTags(
            artist=playlist_metadata["attributes"].get("curatorName", "Unknown"),
            playlist_id=playlist_metadata["attributes"]["playParams"]["id"],
            title=playlist_metadata["attributes"]["name"],
            track=playlist_track,
        )

        log.debug("success", playlist_tags=playlist_tags)

        return playlist_tags

    async def get_tags_from_asset_info(
        self,
        asset_data: dict,
        lyrics: str | None = None,
        use_album_date: bool = False,
    ) -> MediaTags:
        log = logger.bind(
            action="get_tags_from_asset_info", asset_id=asset_data["itemId"]
        )

        tags = MediaTags(
            album=asset_data.get("playlistName"),
            album_artist=asset_data.get("playlistArtistName"),
            album_id=(
                int(asset_data["playlistId"]) if asset_data.get("playlistId") else None
            ),
            album_sort=asset_data.get("sort-album"),
            artist=asset_data["artistName"],
            artist_id=(
                int(asset_data["artistId"]) if asset_data.get("artistId") else None
            ),
            artist_sort=asset_data["sort-artist"],
            comment=asset_data.get("comments"),
            compilation=asset_data.get("compilation"),
            composer=asset_data.get("composerName"),
            composer_id=(
                int(asset_data.get("composerId"))
                if asset_data.get("composerId")
                else None
            ),
            composer_sort=asset_data.get("sort-composer"),
            copyright=asset_data.get("copyright"),
            date=(
                await self.get_media_date(asset_data["playlistId"])
                if use_album_date
                else (
                    self.parse_date(asset_data["releaseDate"])
                    if asset_data.get("releaseDate")
                    else None
                )
            ),
            disc=asset_data.get("discNumber"),
            disc_total=asset_data.get("discCount"),
            gapless=asset_data.get("gapless"),
            genre=asset_data.get("genre"),
            genre_id=(
                int(asset_data["genreId"]) if asset_data.get("genreId") else None
            ),
            lyrics=lyrics if lyrics else None,
            media_type=(
                MediaType.SONG
                if asset_data["kind"] == "song"
                else MediaType.MUSIC_VIDEO
            ),
            rating=MediaRating(asset_data["explicit"]),
            storefront=(int(asset_data["s"]) if asset_data.get("s") else None),
            title=asset_data["itemName"],
            title_id=int(asset_data["itemId"]),
            title_sort=asset_data["sort-name"],
            track=asset_data.get("trackNumber"),
            track_total=asset_data.get("trackCount"),
            xid=asset_data.get("xid"),
        )

        log.debug("success", tags=tags)

        return tags
