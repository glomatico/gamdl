"""Compatibility imports for the API HTTP transport.

New code should import from :mod:`gamdl.transport`.
"""

from gamdl.transport.private import ApiHttpClient, RequestPacer

__all__ = ["ApiHttpClient", "RequestPacer"]
