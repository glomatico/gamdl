"""Python-facing wrapper around the Gamdl Rust MP4 tagger."""

from __future__ import annotations

from dataclasses import asdict
from os import PathLike, fspath

from gamdl import _native
from gamdl.download.tags import Tags


class Muxer:
    """Native operations on MP4 containers.

    Native audio remuxing operates on bytes and preserves encoded packets.
    Tagging operates on an MP4 file. Tag temporary outputs before publishing
    the final path.
    """

    def remux(self, data: bytes) -> bytes:
        """Flatten decrypted, single-track fragmented MP4 audio without transcoding.

        Preserves compressed samples and codec configuration. Returns an
        ordinary seekable MP4 in memory; unsupported input fails closed.
        Call after native Stream decryption, before optional file tagging.
        """
        if not isinstance(data, bytes):
            raise TypeError("data must be MP4 bytes")
        return _native.remux_mp4(data)

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
