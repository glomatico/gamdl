from ..utils import GamdlError


class GamdlDownloaderError(GamdlError):
    pass


class GamdlDownloaderSyncedLyricsOnlyError(GamdlDownloaderError):
    def __init__(self) -> None:
        super().__init__("Download mode is set to synced lyrics only")


class GamdlDownloaderMediaFileExistsError(GamdlDownloaderError):
    def __init__(self, file_path: str) -> None:
        super().__init__(f"Media file already exists: {file_path}")


class GamdlDownloaderDependencyNotFoundError(GamdlDownloaderError):
    def __init__(self, dependency_name: str) -> None:
        super().__init__(f"Required dependency not found: {dependency_name}")
