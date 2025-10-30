class GamdlNotStreamableError(Exception):
    def __init__(self):
        super().__init__("Media is not streamable")


class GamdlFormatNotAvailableError(Exception):
    def __init__(self):
        super().__init__("Media is not available in the requested format")


class GamdlExecutableNotFoundError(Exception):
    def __init__(self, executable: str):
        super().__init__(f"{executable} was not found in system PATH")


class GamdlSyncedLyricsOnlyError(Exception):
    def __init__(self):
        super().__init__(
            "Cannot download media because downloader is configured to download "
            "synced lyrics only"
        )
