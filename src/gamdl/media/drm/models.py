"""Redacted sensitive key descriptors returned to authorized callers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, repr=False)
class ContentKey:
    """A license's content key and 16-byte identifier (both kept in memory).

    The default repr deliberately never prints sensitive key material. Key
    bytes are returned for callers that need them in their media pipeline.
    """

    kid: bytes
    key: bytes

    def __repr__(self) -> str:
        return "ContentKey(<redacted>)"
