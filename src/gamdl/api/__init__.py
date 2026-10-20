"""Asynchronous Apple Music, iTunes and wrapper clients."""

from .apple_music import AppleMusicApi
from .exceptions import (
    GamdlApiError,
    GamdlApiResponseError,
    GamdlAuthenticationError,
    GamdlRateLimitError,
    GamdlTransportError,
)
from .http import ApiHttpClient, RequestPacer
from .itunes import ItunesApi
from .wrapper import WrapperApi

__all__ = [
    "ApiHttpClient",
    "AppleMusicApi",
    "GamdlApiError",
    "GamdlApiResponseError",
    "GamdlAuthenticationError",
    "GamdlRateLimitError",
    "GamdlTransportError",
    "ItunesApi",
    "RequestPacer",
    "WrapperApi",
]
