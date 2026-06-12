import json
from typing import Any

from ..utils import GamdlError


class GamdlApiError(GamdlError):
    pass


class GamdlApiResponseError(GamdlApiError):
    def __init__(
        self,
        message: str,
        content: Any | None = None,
        status_code: int | None = None,
    ):
        self.message = message
        self.content = content
        self.status_code = status_code

        if status_code is not None:
            message = f"{message} (Status code: {status_code})"

        if content is not None:
            if isinstance(content, str):
                content_text = content
            else:
                try:
                    content_text = json.dumps(content)
                except TypeError:
                    content_text = str(content)

            if content_text:
                message += f": {content_text}"

        super().__init__(message)
