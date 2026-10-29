"""Python-facing wrapper around the Gamdl Rust MP4 tagger."""

from __future__ import annotations

from dataclasses import asdict
from os import PathLike, fspath
from typing import Literal

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
        ordinary seekable M4A in memory; unsupported input fails closed.
        Call after native Streamer decryption, before optional file tagging.
        """
        if not isinstance(data, bytes):
            raise TypeError("data must be MP4 bytes")
        return _native.remux_mp4(data)

    def remux_file(
        self, source: str | PathLike[str], destination: str | PathLike[str]
    ) -> None:
        """Remux staged fMP4 audio from disk to disk with bounded memory."""
        _native.remux_mp4_file(fspath(source), fspath(destination))

    def mux(
        self,
        *,
        video: bytes,
        audio: bytes,
        container: Literal["m4v", "mp4"] = "m4v",
    ) -> bytes:
        """Combine decrypted fMP4 video and audio into a seekable M4V/MP4.

        Lossless: encoded packets are copied, never transcoded. Preserves
        supported clear caption tracks embedded in the selected video stream.
        Defaults to an M4V-branded file. Use container="mp4" for the
        standard mp42 brand; encoded audio/video codecs remain unchanged.
        Returns bytes, suitable for writing or sending to another consumer.
        DRM removal must have already happened in separate Streamer calls.
        """
        if not isinstance(video, bytes) or not isinstance(audio, bytes):
            raise TypeError("video and audio must be MP4 bytes")
        if container not in ("m4v", "mp4"):
            raise ValueError("container must be 'm4v' or 'mp4'")
        return _native.mux_av_mp4(video, audio, container == "mp4")

    def mux_file(
        self,
        *,
        video: str | PathLike[str],
        audio: str | PathLike[str],
        destination: str | PathLike[str],
        container: Literal["m4v", "mp4"] = "m4v",
    ) -> None:
        """Mux staged video/audio to a completed MP4 without loading full tracks."""
        if container not in ("m4v", "mp4"):
            raise ValueError("container must be 'm4v' or 'mp4'")
        _native.mux_av_mp4_file(
            fspath(video), fspath(audio), fspath(destination), container == "mp4"
        )

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
        if fspath(path).lower().endswith(".mp3"):
            _native.write_mp3_tags(fspath(path), asdict(tags), cover_bytes)
        else:
            _native.write_mp4_tags(fspath(path), asdict(tags), cover_bytes)
