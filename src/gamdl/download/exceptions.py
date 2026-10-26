"""Download preparation errors."""

from gamdl.exceptions import GamdlError


class TagResolutionError(GamdlError):
    """Output metadata could not be derived from the available sources."""


class DownloadFinalizationError(GamdlError):
    """The media file exists, but one of its post-publication steps failed."""

    def __init__(self, path, stage: str) -> None:
        self.path = path
        self.stage = stage
        super().__init__(
            f"Media is available at {path}, but {stage} finalization failed; "
            "retry with overwrite='skip'"
        )
