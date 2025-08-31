class MediaNotStreamableException(Exception):
    def __init__(self, message: str = "Media is not streamable"):
        super().__init__(message)


class MediaFileAlreadyExistsException(Exception):
    def __init__(self, message: str = "Media file already exists"):
        super().__init__(message)


class MediaFormatNotAvailableException(Exception):
    def __init__(self, message: str = "Requested media format or codec not available"):
        super().__init__(message)
