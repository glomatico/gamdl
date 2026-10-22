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


class StreamManifestError(GamdlMediaError):
    """A media CDN returned an invalid or unsupported streaming playlist."""


class DrmError(GamdlMediaError):
    """An authorized DRM operation could not be completed."""


class DrmConfigurationError(DrmError):
    """No provisioned device/CDM was supplied or the backend is unavailable."""


class DrmMetadataError(DrmError):
    """The selected stream has no usable DRM signaling for this backend."""


class DrmLicenseError(DrmError):
    """The DRM license exchange or CDM processing failed."""
