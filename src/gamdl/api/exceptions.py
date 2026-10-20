"""Public, sanitized API exception types."""

from ..exceptions import GamdlError


class GamdlApiError(GamdlError):
    """Base error raised for a remote API operation.

    Only safe metadata is attached; raw response bodies and credentials are not.
    """

    def __init__(
        self,
        message: str,
        *,
        service: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.service = service
        self.status_code = status_code


class GamdlApiResponseError(GamdlApiError):
    """The service returned an invalid response or an unsuccessful status."""


class GamdlAuthenticationError(GamdlApiError):
    """Missing, expired or rejected authentication information."""


class GamdlRateLimitError(GamdlApiResponseError):
    """The service refused a request due to rate limiting."""

    def __init__(
        self,
        message: str = "API rate limit exceeded",
        *,
        service: str | None = None,
        status_code: int = 429,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, service=service, status_code=status_code)
        self.retry_after = retry_after


class GamdlTransportError(GamdlApiError):
    """A network connection or transport operation failed."""
