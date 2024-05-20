from __future__ import annotations

from pathlib import Path

from InquirerPy import inquirer
from InquirerPy.base.control import Choice

from .downloader import Downloader
from .enums import PostQuality


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

    def get_tags(self, metadata: dict) -> list:
        attributes = metadata["attributes"]
        return {
            "artist": attributes["artistName"],
            "date": self.downloader.sanitize_date(attributes["uploadDate"]),
            "title": attributes["name"],
            "title_id": int(metadata["id"]),
            "storefront": int(self.downloader.itunes_api.storefront_id.split("-")[0]),
        }

    def get_post_temp_path(self, track_id: str) -> Path:
        return self.downloader.temp_path / f"{track_id}_temp.m4v"
