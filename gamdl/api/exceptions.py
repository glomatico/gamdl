from ..utils import GamdlError


class ApiError(GamdlError):
    def __init__(self, message: str, status_code: int):
        super().__init__(f"API Error {status_code}: {message}")
        self.status_code = status_code
