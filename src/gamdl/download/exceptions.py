"""Download preparation errors."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import DownloadResult

from gamdl.exceptions import GamdlError


class TagResolutionError(GamdlError):
    """Output metadata could not be derived from the available sources."""


class DownloadFinalizationError(GamdlError):
    """The media file exists, but one of its post-publication steps failed."""

    def __init__(
        self, path: Path, stage: str, partial_result: DownloadResult | None = None
    ) -> None:
        self.path = path
        self.stage = stage
        self.partial_result = partial_result
        super().__init__(
            f"Media is available at {path}, but {stage} finalization failed; "
            "retry with overwrite='skip'"
        )
