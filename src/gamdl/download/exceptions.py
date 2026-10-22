"""Download preparation errors."""

from gamdl.exceptions import GamdlError


class TagResolutionError(GamdlError):
    """Output metadata could not be derived from the available sources."""
