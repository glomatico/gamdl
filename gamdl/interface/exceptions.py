from ..utils import GamdlError
from typing import Any


class GamdlInterfaceError(GamdlError):
    pass


class GamdlInterfaceMediaNotStreamableError(GamdlInterfaceError):
    def __init__(self, media_id: str):
        super().__init__(f"Media is not streamable: {media_id}")


class GamdlInterfaceFormatNotAvailableError(GamdlInterfaceError):
    def __init__(self, media_id: str, codec: Any | None = None):
        super().__init__(
            f"Requested format is not available (media ID: {media_id}): {codec}"
        )


class GamdlInterfaceDecryptionNotAvailableError(GamdlInterfaceError):
    def __init__(self, media_id: str):
        super().__init__(f"Decryption is not available for media ID: {media_id}")


class GamdlInterfaceMediaNotAllowedError(GamdlInterfaceError):
    def __init__(self, media_type: str, media_id: str | None = None):
        message = "Media type is disallowed"
        if media_id:
            message += f" (media ID: {media_id})"

        super().__init__(f"{message}: {media_type}")


class GamdlInterfaceUrlParseError(GamdlInterfaceError):
    def __init__(self, url: str):
        super().__init__(f"URL is not valid or supported: {url}")


class GamdlInterfaceArtistMediaTypeError(GamdlInterfaceError):
    def __init__(self, media_type: str):
        super().__init__(f"Artist has no media of type: {media_type}")
