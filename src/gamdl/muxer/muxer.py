"""Python-facing wrapper around the Gamdl Rust MP4 tagger."""

from __future__ import annotations

from dataclasses import asdict
from os import PathLike, fspath

from gamdl import _native
from gamdl.download.tags import Tags


class Muxer:
    """Native operations on MP4 containers.

    The initial milestone implements in-place metadata tagging. Later native
    decryption/muxing operations will join this interface. Tag files in the
    download pipeline's temporary output, before publishing the final path.
    """

    def tag(
        self,
        path: str | PathLike[str],
        *,
        tags: Tags,
        cover_bytes: bytes | None = None,
    ) -> None:
        """Write supplied fields and optional JPEG/PNG artwork to an MP4 file.

        Fields set to None are preserved if the file already has those atoms.
        A non-MP4 file or invalid cover raises an exception; no transcoding.
        """
        if not isinstance(tags, Tags):
            raise TypeError("tags must be a gamdl.download.Tags instance")
        _native.write_mp4_tags(fspath(path), asdict(tags), cover_bytes)
