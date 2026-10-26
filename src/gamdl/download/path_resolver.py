"""Filesystem-aware collision handling, separate from pure PathFormatter."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from gamdl import _native

if TYPE_CHECKING:
    from gamdl.media import Media
    from .database import MediaDatabase


def _numbered_video_path(path: Path, index: int) -> Path:
    """Use the GAMDL 3.x `Title 2.m4v` scheme, respecting UTF-8 name limits."""
    if index == 1:
        return path
    suffix = f" {index}{path.suffix}"
    available = 255 - len(suffix.encode("utf-8"))
    if available < 1:
        raise ValueError("Cannot add a collision suffix to this filename")
    stem = path.stem
    while len(stem.encode("utf-8")) > available:
        stem = stem[:-1]
    if not stem:
        stem = "_"
    return path.with_name(stem + suffix)


def _index_for_path(base: Path, candidate: Path) -> int | None:
    if candidate.name == base.name:
        return 1
    if candidate.suffix != base.suffix:
        return None
    # Avoid interpreting arbitrary `Title 02.m4v` and `Title 1.m4v` as
    # generated variants. GAMDL 3.x starts numbered suffixes at 2.
    match = re.search(r" ((?:[2-9]|[1-9][0-9]+))$", candidate.stem)
    if match is None or len(match.group(1)) > 12:
        return None
    index = int(match.group(1))
    return index if _numbered_video_path(base, index).name == candidate.name else None


def resolve_numbered_video_path(
    path: Path,
    media: Media,
    *,
    database: MediaDatabase | None = None,
) -> Path:
    """Reuse a same-ID file or assign the lowest available numbered filename.

    An SQLite record takes precedence when present; otherwise consult native
    MP4 `cnID` tags, just as GAMDL 3.x consulted Mutagen. Unrecognized files
    occupy their slots but are never claimed or overwritten as a match.

    This resolver does not write files. `publish_bytes` still owns the atomic
    no-overwrite guarantee when another process is downloading concurrently.
    """
    if not path.parent.exists():
        return path
    candidates: dict[int, Path] = {}
    for candidate in path.parent.iterdir():
        if not (candidate.is_file() or candidate.is_symlink() or candidate.is_dir()):
            continue
        index = _index_for_path(path, candidate)
        if index is not None:
            candidates[index] = candidate

    for index, candidate in sorted(candidates.items()):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        record = database.get(candidate) if database else None
        if record is not None:
            if record["media_id"] == media.id and record["media_type"] == media.type:
                return candidate
        elif _native.read_mp4_title_id(str(candidate)) == media.id:
            return candidate

    index = 1
    while index in candidates:
        index += 1
    return _numbered_video_path(path, index)
