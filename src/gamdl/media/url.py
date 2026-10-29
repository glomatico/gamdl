"""Parse Apple Music sharing links into structured, validated URL information.

The URL pattern is based on Gamdl 3.x's ``VALID_URL_PATTERN``. In addition to
its catalog, Classical and library links, Gamdl 4.x accepts the explicit
``uploaded-video`` alias and a bare ``/library`` browser link.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs

from .exceptions import UnsupportedMediaError

VALID_URL_PATTERN = re.compile(
    r"https://(?:classical\.)?music\.apple\.com"
    r"(?:"
    r"/(?P<storefront>[a-z]{2})"
    r"/(?P<type>artist|album|playlist|song|music-video|post|uploaded-video)"
    r"(?:/(?P<slug>[^\s/?#]+))?"
    r"/(?P<id>[0-9]+|pl\.[0-9a-z]{32}|pl\.u-[a-zA-Z0-9]+)"
    r"|"
    r"(?:/(?P<library_storefront>[a-z]{2}))?"
    r"/library"
    r"(?:/(?P<library_type>playlist|albums|songs|music-videos)"
    r"/(?P<library_id>[pli]\.[a-zA-Z0-9]+))?"
    r")"
    r"/?(?:\?(?P<query>[^#]*))?(?:#.*)?\Z"
)


@dataclass(frozen=True, slots=True)
class AppleMusicUrlInfo:
    """A parsed Apple Music URL; ``parse`` rejects incomplete or unknown links.

    ``type`` / ``id`` refer to catalog resources. Library resources instead
    populate ``library_type`` / ``library_id``. ``sub_id`` is the optional
    catalog song ID carried by an ``i=`` query parameter.
    """

    storefront: str | None = None
    type: str | None = None
    slug: str | None = None
    id: str | None = None
    sub_id: str | None = None
    library_storefront: str | None = None
    library_type: str | None = None
    library_id: str | None = None

    @classmethod
    def parse(cls, url: str) -> AppleMusicUrlInfo:
        """Parse an Apple Music or Apple Music Classical link without network I/O.

        Uses an anchored form of Gamdl 3.x's URL regex, so malformed paths and
        URLs from lookalike hosts are not silently accepted as valid media.
        """
        match = VALID_URL_PATTERN.fullmatch(url.strip())
        if match is None:
            raise UnsupportedMediaError("Invalid or unsupported Apple Music URL")

        fields = match.groupdict()
        query = fields.pop("query") or ""
        sub_ids = (
            parse_qs(query, keep_blank_values=True).get("i", [])
            if fields["type"]
            else []
        )
        if sub_ids and not re.fullmatch(r"[0-9]+", sub_ids[0]):
            raise UnsupportedMediaError("Invalid Apple Music track ID in URL")
        sub_id = sub_ids[0] if sub_ids else None
        return cls(**fields, sub_id=sub_id)

    @property
    def is_library(self) -> bool:
        """Whether this link targets a user's library (including its root)."""
        return self.type is None

    @property
    def kind(self) -> str:
        """Normalized media type shared by catalog and library links."""
        if self.type is not None:
            return "uploaded-video" if self.type == "post" else self.type
        return {
            None: "library",
            "playlist": "playlist",
            "albums": "album",
            "songs": "song",
            "music-videos": "music-video",
        }[self.library_type]

    @property
    def media_id(self) -> str | None:
        """Catalog or library resource identifier, if one was supplied."""
        return self.id or self.library_id
