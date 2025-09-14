from __future__ import annotations

import logging
import typing
from pathlib import Path

import colorama
from InquirerPy import inquirer
from InquirerPy.base.control import Choice

from .downloader import Downloader
from .enums import PostQuality
from .exceptions import MediaFileAlreadyExistsException, MediaNotStreamableException
from .models import DownloadInfo, MediaTags
from .utils import color_text

logger = logging.getLogger("gamdl")


class DownloaderPost:
    QUALITY_RANK = [
        "1080pHdVideo",
        "720pHdVideo",
        "sdVideoWithPlusAudio",
        "sdVideo",
        "sd480pVideo",
        "provisionalUploadVideo",
    ]

    def __init__(
        self,
        downloader: Downloader,
        quality: PostQuality = PostQuality.BEST,
    ):
        self.downloader = downloader
        self.quality = quality

    def get_stream_url_best(self, metadata: dict) -> str:
        best_quality = next(
            (
                quality
                for quality in self.QUALITY_RANK
                if metadata["attributes"]["assetTokens"].get(quality)
            ),
            None,
        )
        return metadata["attributes"]["assetTokens"][best_quality]

    def get_stream_url_from_user(self, metadata: dict) -> str:
        qualities = list(metadata["attributes"]["assetTokens"].keys())
        choices = [
            Choice(
                name=quality,
                value=quality,
            )
            for quality in qualities
        ]
        selected = inquirer.select(
            message="Select which quality to download:",
            choices=choices,
        ).execute()
        return metadata["attributes"]["assetTokens"][selected]

    def get_stream_url(self, metadata: dict) -> str:
        if self.quality == PostQuality.BEST:
            stream_url = self.get_stream_url_best(metadata)
        elif self.quality == PostQuality.ASK:
            stream_url = self.get_stream_url_from_user(metadata)
        return stream_url

    def get_tags(self, metadata: dict) -> MediaTags:
        attributes = metadata["attributes"]
        upload_date = attributes.get("uploadDate")
        return MediaTags(
            artist=attributes.get("artistName"),
            date=self.downloader.parse_date(upload_date) if upload_date else None,
            title=attributes.get("name"),
            title_id=int(metadata["id"]),
            storefront=int(self.downloader.itunes_api.storefront_id.split("-")[0]),
        )

    def get_cover_path(self, final_path: Path, cover_format: str) -> Path:
        return final_path.with_suffix(
            self.downloader.get_cover_file_extension(cover_format)
        )

    def download(
        self,
        media_id: str = None,
        media_metadata: dict = None,
    ) -> typing.Generator[DownloadInfo, None, None]:
        yield from self.downloader._final_processing_wrapper(
            self._download,
            media_id,
            media_metadata,
        )

    def _download(
        self,
        media_id: str = None,
        media_metadata: dict = None,
    ) -> typing.Generator[DownloadInfo, None, None]:
        download_info = DownloadInfo()
        yield download_info

        if not media_id and not media_metadata:
            raise ValueError("Either media_id or media_metadata must be provided")

        if media_metadata:
            media_id = media_metadata["id"]
        download_info.media_id = media_id
        colored_media_id = color_text(media_id, colorama.Style.DIM)

        database_final_path = self.downloader.get_database_final_path(media_id)
        if database_final_path:
            download_info.final_path = database_final_path
            yield download_info
            raise MediaFileAlreadyExistsException(database_final_path)

        if not media_metadata:
            logger.debug(f"[{colored_media_id}] Getting Post Video metadata")
            media_metadata = self.downloader.apple_music_api.get_post(media_id)
        download_info.media_metadata = media_metadata

        if not self.downloader.is_media_streamable(media_metadata):
            yield download_info
            raise MediaNotStreamableException()

        tags = self.get_tags(media_metadata)
        final_path = self.downloader.get_final_path(
            tags,
            ".m4v",
            None,
        )
        download_info.tags = tags
        download_info.final_path = final_path

        if final_path.exists() and not self.downloader.overwrite:
            yield download_info
            raise MediaFileAlreadyExistsException(final_path)

        cover_url = self.downloader.get_cover_url(media_metadata)
        cover_format = self.downloader.get_cover_format(cover_url)
        if cover_format and self.downloader.save_cover:
            cover_path = self.get_cover_path(final_path, cover_format)
        else:
            cover_path = None
        download_info.cover_url = cover_url
        download_info.cover_format = cover_format
        download_info.cover_path = cover_path

        stream_url = self.get_stream_url(media_metadata)
        staged_path = self.downloader.get_temp_path(
            media_id,
            "stage",
            ".m4v",
        )

        logger.info(f"[{colored_media_id}] Downloading Post Video")

        logger.debug(f"[{colored_media_id}] Downloading to {staged_path}")
        self.downloader.download_ytdlp(
            staged_path,
            stream_url,
        )
        download_info.staged_path = staged_path

        yield download_info
