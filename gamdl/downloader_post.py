from pathlib import Path

import click

from .downloader import Downloader
from tabulate import tabulate
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
        table = [
            [index, quality]
            for index, quality in enumerate(
                qualities,
                start=1,
            )
        ]
        print(tabulate(table))
        choice = (
            click.prompt("Choose a quality", type=click.IntRange(1, len(table))) - 1
        )
        return metadata["attributes"]["assetTokens"][qualities[choice]]

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
            "date": attributes["uploadDate"],
            "title": attributes["name"],
            "title_id": int(metadata["id"]),
        }

    def get_temp_path(self, track_id: str) -> Path:
        return self.downloader.temp_path / f"{track_id}_temp.m4v"
