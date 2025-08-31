from __future__ import annotations

from pathlib import Path


class MediaNotStreamableException(Exception):
    DEFAULT_MESSAGE = "Media is not streamable"

    def __init__(self):
        super().__init__(self.DEFAULT_MESSAGE)


class MediaFileAlreadyExistsException(Exception):
    DEFAULT_MESSAGE = "Media file already exists at '{media_path}'"

    def __init__(self, media_path: Path):
        super().__init__(self.DEFAULT_MESSAGE.format(media_path=media_path))


class MediaFormatNotAvailableException(Exception):
    DEFAULT_MESSAGE = "Requested media format or codec not available"

    def __init__(self):
        super().__init__(self.DEFAULT_MESSAGE)
