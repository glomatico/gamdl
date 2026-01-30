from ..utils import GamdlError


class MediaFileExists(GamdlError):
    def __init__(self, media_path: str):
        super().__init__(f"Media file already exists at path: {media_path}")


class NotStreamable(GamdlError):
    def __init__(self, media_id: str):
        super().__init__(f"Media ID is not streamable: {media_id}")


class FormatNotAvailable(GamdlError):
    def __init__(self, media_id: str):
        super().__init__(f"Requested format is not available for media ID: {media_id}")


class ExecutableNotFound(GamdlError):
    def __init__(self, executable: str):
        super().__init__(f"Executable not found: {executable}")


class SyncedLyricsOnly(GamdlError):
    def __init__(self):
        super().__init__("Only downloading synced lyrics is supported")


class UnsupportedMediaType(GamdlError):
    def __init__(self, media_type: str):
        super().__init__(f"Unsupported media type: {media_type}")
