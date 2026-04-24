from ..utils import GamdlError


class GamdlApiError(GamdlError):
    pass


class GamdlApiResponseError(GamdlApiError):
    def __init__(
        self,
        message: str,
        content: str | None = None,
        status_code: int | None = None,
    ):
        self.message = message
        self.content = content
        self.status_code = status_code

        if status_code is not None:
            message = f"{message} (Status code: {status_code})"

        if content:
            message += f": {content}"

        super().__init__(message)
