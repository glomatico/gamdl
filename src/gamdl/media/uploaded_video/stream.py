"""Lazy quality selection for progressive Apple Music uploaded videos."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

from ..enums import StreamOrigin
from ..exceptions import SelectionRequiredError, StreamNotAvailableError
from ..stream import HttpStream


class UploadedVideoQuality(str, Enum):
    """Apple Music asset-token variants in GAMDL 3.x preference order."""

    HD_1080P = "1080pHdVideo"
    HD_720P = "720pHdVideo"
    SD_PLUS_AUDIO = "sdVideoWithPlusAudio"
    SD = "sdVideo"
    SD_480P = "sd480pVideo"
    PROVISIONAL = "provisionalUploadVideo"


DEFAULT_QUALITY_PRIORITY = tuple(UploadedVideoQuality)


@dataclass(frozen=True)
class UploadedVideoSelectionOption:
    index: int
    quality: UploadedVideoQuality


@dataclass(frozen=True)
class UploadedVideoSelectionRequest:
    options: tuple[UploadedVideoSelectionOption, ...]


UploadedVideoStreamSelector = Callable[
    [UploadedVideoSelectionRequest], int | Awaitable[int]
]


class UploadedVideoStreamResolver:
    """Choose an available direct HTTP asset without transferring any bytes."""

    def __init__(self, media) -> None:
        self.media = media

    async def get_stream(
        self,
        *,
        quality: UploadedVideoQuality | None = None,
        quality_priority: Sequence[UploadedVideoQuality] | None = None,
        ask: bool = False,
        selector: UploadedVideoStreamSelector | None = None,
    ) -> HttpStream:
        if quality is not None and quality_priority is not None:
            raise ValueError("Provide either quality or quality_priority, not both")
        if ask:
            if quality is not None or quality_priority is not None:
                raise ValueError("ask cannot be combined with quality preferences")
            if selector is None:
                raise SelectionRequiredError("uploaded_video_quality")
            priorities = DEFAULT_QUALITY_PRIORITY
        else:
            if selector is not None:
                raise ValueError("selector requires ask=True")
            try:
                if isinstance(quality_priority, (str, bytes)):
                    raise ValueError
                priorities = (
                    (UploadedVideoQuality(quality),)
                    if quality is not None
                    else tuple(UploadedVideoQuality(q) for q in quality_priority)
                    if quality_priority is not None
                    else DEFAULT_QUALITY_PRIORITY
                )
            except (ValueError, TypeError):
                raise ValueError("Unknown uploaded-video quality preference") from None
            if not priorities:
                raise ValueError("quality_priority must not be empty")

        async def load() -> HttpStream:
            metadata = await self.media.get_metadata()
            attributes = metadata.get("attributes", {})
            tokens = (
                attributes.get("assetTokens", {})
                if isinstance(attributes, dict)
                else {}
            )
            if not isinstance(tokens, dict):
                tokens = {}
            options: list[tuple[UploadedVideoQuality, str]] = []
            for variant in priorities:
                value = tokens.get(variant.value)
                if not isinstance(value, str) or not value:
                    continue
                parts = urlsplit(value)
                if (
                    parts.scheme != "https"
                    or not parts.hostname
                    or parts.username is not None
                    or parts.password is not None
                ):
                    continue
                options.append((variant, value))
            if not options:
                raise StreamNotAvailableError(
                    "No compatible uploaded-video asset is available"
                )
            chosen = 0
            if ask:
                assert selector is not None
                response = selector(
                    UploadedVideoSelectionRequest(
                        tuple(
                            UploadedVideoSelectionOption(i, item[0])
                            for i, item in enumerate(options)
                        )
                    )
                )
                if inspect.isawaitable(response):
                    response = await response
                if (
                    type(response) is not int
                    or response < 0
                    or response >= len(options)
                ):
                    raise ValueError("Selector returned an invalid quality index")
                chosen = response
            variant, url = options[chosen]
            return HttpStream(
                url,
                origin=StreamOrigin.UPLOADED_VIDEO,
                codec=variant,
                media_id=self.media.id,
            )

        if ask:
            return await load()
        return await self.media._load(("uploaded_video_stream", priorities), load)
