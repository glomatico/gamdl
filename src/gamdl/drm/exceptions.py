"""DRM-specific failures, independent of media types."""

from gamdl.exceptions import GamdlError


class DrmError(GamdlError):
    """A DRM operation could not be completed."""


class DrmConfigurationError(DrmError):
    """No valid backend, device or content-decryption module is configured."""


class DrmMetadataError(DrmError):
    """DRM signaling cannot initialize the configured backend."""


class DrmLicenseError(DrmError):
    """The license exchange or CDM processing failed."""
