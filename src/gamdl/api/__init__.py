"""Asynchronous Apple Music, iTunes and wrapper clients."""

from gamdl.transport import ApiHttpClient, RequestPacer

from .apple_music import AppleMusicApi
from .exceptions import (
    GamdlApiError,
    GamdlApiResponseError,
    GamdlAuthenticationError,
    GamdlRateLimitError,
    GamdlTransportError,
)
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
