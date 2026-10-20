"""Lightweight, quiet structlog integration for the reusable library.

Do not configure the global logging or structlog registries from a package import.
Host applications decide whether and where to display diagnostic logs.
"""

import logging

import structlog


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a contextual logger backed by standard-library logging."""
    return structlog.wrap_logger(
        logging.getLogger(name),
        wrapper_class=structlog.stdlib.BoundLogger,
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.processors.KeyValueRenderer(key_order=["event"]),
        ],
    )
