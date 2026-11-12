"""Gamdl: an asynchronous Apple Music toolkit."""

from importlib.metadata import PackageNotFoundError, version
import logging

from .exceptions import GamdlError

logging.getLogger(__name__).addHandler(logging.NullHandler())

try:
    __version__ = version("gamdl")
except PackageNotFoundError:
    __version__ = "4.0.0"

__all__ = ["GamdlError", "__version__"]
