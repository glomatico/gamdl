"""Incremental, idempotent M3U playlist updates for published local media."""

from __future__ import annotations

import os
import re
import threading
import weakref
from pathlib import Path

from .paths import PlaylistTags
from .publication import publish_bytes

_registry_lock = threading.Lock()
_playlist_locks: weakref.WeakValueDictionary[Path, threading.RLock] = (
    weakref.WeakValueDictionary()
)
_HEADER_FIELDS = (
    ("#PLAYLIST:", "title"),
    ("#Gamdl-PLAYLIST-ARTIST:", "artist"),
    ("#Gamdl-APPLE-MUSIC-ID:", "playlist_id"),
)


def _header_value(value: str) -> str:
    """Keep metadata on one M3U comment line, including unusual Unicode."""
    return re.sub(r"[\x00-\x1f\x7f\x85\u2028\u2029]", " ", value).strip()


def split_playlist(lines: list[str]) -> tuple[list[str], list[str]]:
    """Split an extended M3U into leading comments and track entries.

    The leading blank line is a valid empty track slot, not a header. EXTINF
    belongs to the entry it describes, so it ends the header block as well.
    """
    if not lines or lines[0] != "#EXTM3U":
        raise ValueError("Existing playlist is not an #EXTM3U playlist")
    offset = 1
    while (
        offset < len(lines)
        and lines[offset].startswith("#")
        and not lines[offset].startswith("#EXTINF:")
    ):
        offset += 1
    return lines[1:offset], lines[offset:]


def playlist_tags_from_header(header: list[str]) -> PlaylistTags:
    values: dict[str, str] = {}
    for line in header:
        for prefix, field in _HEADER_FIELDS:
            # Older files used #GAMDL-...; read both spellings.
            if (
                line[: len(prefix)].casefold() == prefix.casefold()
                and field not in values
            ):
                values[field] = line[len(prefix) :]
    return PlaylistTags(**values)


def _update_header(header: list[str], tags: PlaylistTags | None) -> list[str]:
    # Normalize older branding when a playlist is written, even if only its
    # entries changed. The parser remains compatible with historical headers.
    updated = header.copy()
    for i, line in enumerate(updated):
        for prefix, _ in _HEADER_FIELDS:
            if line[: len(prefix)].casefold() == prefix.casefold():
                updated[i] = prefix + line[len(prefix) :]
                break
    if tags is None:
        return updated
    for prefix, field in _HEADER_FIELDS:
        value = getattr(tags, field)
        if value is None:
            continue
        rendered = _header_value(str(value))
        if not rendered:
            continue
        line = prefix + rendered
        for i, previous in enumerate(updated):
            if previous[: len(prefix)].casefold() == prefix.casefold():
                updated[i] = line
                break
        else:
            updated.append(line)
    return updated


def _lock_for(path: Path) -> threading.RLock:
    """Reuse per-path locks without growing a permanent global path registry."""
    with _registry_lock:
        key = path.absolute()
        lock = _playlist_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _playlist_locks[key] = lock
        return lock


class PlaylistWriter:
    """Write relative M3U paths; preserve ordered repeated playlist entries.

    ``position`` is zero-based and keeps duplicate song occurrences distinct.
    When no position is known, appending the same path twice is suppressed.
    In-process writers to the same playlist are serialized across threads.
    Cross-process writers must still coordinate externally.
    """

    def __init__(self, path: str | Path, *, tags: PlaylistTags | None = None) -> None:
        self.path = Path(path)
        self.tags = tags

    def add(self, media_file: str | Path, *, position: int | None = None) -> bool:
        with _lock_for(self.path):
            return self._add_unlocked(media_file, position=position)

    def _add_unlocked(
        self, media_file: str | Path, *, position: int | None = None
    ) -> bool:
        if position is not None and (type(position) is not int or position < 0):
            raise ValueError("playlist position must be a non-negative integer")
        target = Path(media_file).absolute()
        playlist = self.path.absolute()
        value = Path(os.path.relpath(target, playlist.parent)).as_posix()

        if playlist.exists():
            lines = playlist.read_text(encoding="utf-8-sig").splitlines()
            header, entries = split_playlist(lines)
        else:
            header = []
            entries = []
        new_header = _update_header(header, self.tags)

        # Track indices count actual paths (including empty placeholders), not
        # EXTINF or other M3U comments that accompany the entries.
        positions = [i for i, entry in enumerate(entries) if not entry.startswith("#")]
        changed = new_header != header

        if position is None:
            if value not in (entries[i] for i in positions):
                entries.append(value)
                changed = True
        else:
            if position >= len(positions):
                entries.extend("" for _ in range(position + 1 - len(positions)))
                positions = [
                    i for i, entry in enumerate(entries) if not entry.startswith("#")
                ]
            if entries[positions[position]] != value:
                entries[positions[position]] = value
                changed = True

        if not changed:
            return False
        data = ("#EXTM3U\n" + "\n".join([*new_header, *entries]) + "\n").encode("utf-8")
        publish_bytes(data, playlist, overwrite="replace")
        return True
