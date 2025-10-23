class MediaNotStreamableError(Exception):
    def __init__(self, media_id: str):
        super().__init__(
            f'Media with ID "{media_id}" is not streamable'.format(media_id=media_id)
        )


class MediaFormatNotAvailableError(Exception):
    def __init__(self, media_id: str):
        super().__init__(
            f'Media with ID "{media_id}" is not available in the requested format'
        )


class MediaDownloadConfigurationError(Exception):
    def __init__(self, media_id: str):
        super().__init__(
            f'Media with ID "{media_id}" is not downloadable with the current configuration'
        )
