"""Errors from media models and source resolution."""

from ..exceptions import GamdlError


class GamdlMediaError(GamdlError):
    """A media resource cannot be represented or processed."""


class UnsupportedMediaError(GamdlMediaError):
    """The media type or source is not yet supported."""


class SelectionRequiredError(GamdlMediaError):
    """A source requires a selector or an explicit auto-selection policy."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(
            f"Selection required for {kind}; provide a selector or auto_select"
        )


class StreamNotAvailableError(GamdlMediaError):
    """No available stream matches the requested codec or preferences."""


class MediaNotStreamableError(StreamNotAvailableError):
    """Apple Music catalog metadata explicitly lacks playback entitlement."""


class MediaNotFoundError(StreamNotAvailableError):
    """The requested catalog media is absent in the account's storefront."""


class StreamManifestError(GamdlMediaError):
    """A media CDN returned an invalid or unsupported streaming playlist."""
