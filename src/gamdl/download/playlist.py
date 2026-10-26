"""Incremental, idempotent M3U playlist updates for published local media."""

from __future__ import annotations

import os
from pathlib import Path

from .publication import publish_bytes


class PlaylistWriter:
    """Write relative M3U paths; preserve ordered repeated playlist entries.

    ``position`` is zero-based and keeps duplicate song occurrences distinct.
    When no position is known, appending the same path twice is suppressed.
    Writes are atomic, but concurrent writers to the *same* M3U must be
    serialized by the caller to prevent a lost update.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def add(self, media_file: str | Path, *, position: int | None = None) -> bool:
        if position is not None and (type(position) is not int or position < 0):
            raise ValueError("playlist position must be a non-negative integer")
        target = Path(media_file).absolute()
        playlist = self.path.absolute()
        value = Path(os.path.relpath(target, playlist.parent)).as_posix()

        if playlist.exists():
            lines = playlist.read_text(encoding="utf-8-sig").splitlines()
            if not lines or lines[0] != "#EXTM3U":
                raise ValueError("Existing playlist is not an #EXTM3U playlist")
            entries = lines[1:]
        else:
            entries = []

        if position is None:
            if value in entries:
                return False
            entries.append(value)
        else:
            if len(entries) <= position:
                entries.extend("" for _ in range(position + 1 - len(entries)))
            if entries[position] == value:
                return False
            entries[position] = value

        data = ("#EXTM3U\n" + "\n".join(entries) + "\n").encode("utf-8")
        publish_bytes(data, playlist, overwrite="replace")
        return True
