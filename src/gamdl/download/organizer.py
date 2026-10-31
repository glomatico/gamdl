"""Offline library relocation using the same metadata and paths as downloads.

No Apple Music requests, transcoding, tag writes or overwrites. Destination
collisions deliberately remain untouched instead of guessing which copy wins.
"""

from __future__ import annotations

import os
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from gamdl import _native

from .database import MediaDatabase
from .paths import PathFormatter
from .publication import publish_bytes
from .tags import Tags

MEDIA_EXTENSIONS = frozenset({".m4a", ".m4v", ".mp4", ".mp3"})
SIDECAR_EXTENSIONS = (".lrc", ".txt", ".jpg", ".jpeg", ".png")


@dataclass(frozen=True)
class OrganizeMove:
    source: Path
    destination: Path
    sidecars: tuple[tuple[Path, Path], ...]
    media_type: str
    media_id: str | None


@dataclass
class OrganizePlan:
    root: Path
    moves: list[OrganizeMove]
    counts: Counter[str]
    playlists: tuple[Path, ...]
    problems: list[str]


def _exists(path: Path) -> bool:
    # Unlike Path.exists(), broken symlinks must still occupy their filenames.
    return os.path.lexists(path)


def _safe_parents(root: Path, destination: Path) -> bool:
    """Refuse existing symlink directories between root and destination."""
    parent = destination.parent
    while parent != root:
        if not parent.is_relative_to(root) or parent.is_symlink():
            return False
        parent = parent.parent
    return True


def _candidates(root: Path):
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() in MEDIA_EXTENSIONS:
            yield path


def _metadata(path: Path) -> dict:
    if path.suffix.lower() == ".mp3":
        return _native.read_mp3_organize_tags(str(path))
    return _native.read_mp4_organize_tags(str(path))


def _media_type(path: Path, fields: dict, record: dict | None) -> str | None:
    suffix = path.suffix.lower()
    if suffix in {".m4a", ".mp3"}:
        return "songs"
    if suffix == ".m4v":
        return "music-videos"
    # .mp4 is used for both songs and videos. Never guess from its filename.
    if fields.get("media_type") == 6:
        return "music-videos"
    if fields.get("media_type") == 1:
        return "songs"
    if record and record.get("media_type") in {"songs", "music-videos"}:
        return str(record["media_type"])
    return None


def plan_organization(
    root: Path, formatter: PathFormatter, database: MediaDatabase | None = None
) -> OrganizePlan:
    """Inspect metadata and destinations before any files are moved."""
    root = root.absolute()
    counts: Counter[str] = Counter()
    moves: list[OrganizeMove] = []
    problems: list[str] = []
    candidates = sorted(_candidates(root))
    playlists = tuple(
        sorted(
            path
            for path in root.rglob("*.m3u8")
            if path.is_file() and not path.is_symlink()
        )
    )
    for path in candidates:
        counts["scanned"] += 1
        try:
            fields = _metadata(path)
            record = database.get(path) if database is not None else None
            media_type = _media_type(path, fields, record)
            if media_type is None:
                counts["ambiguous"] += 1
                continue
            # Insufficient metadata must not generate Unknown Artist/Title
            # destinations that could silently combine unrelated media.
            if not fields.get("title") or not (
                fields.get("album_artist") or fields.get("artist")
            ):
                counts["untagged"] += 1
                continue
            if fields.get("album") and not (
                fields.get("album_artist") or fields.get("compilation")
            ):
                counts["untagged"] += 1
                continue
            tags = Tags(
                **{
                    key: value
                    for key, value in fields.items()
                    if key in Tags.__dataclass_fields__
                }
            )
            destination = formatter.get_media_path(
                None, tags, extension=path.suffix.lower()
            ).absolute()
            if path == destination:
                counts["unchanged"] += 1
                continue
            if not destination.is_relative_to(root):
                raise ValueError("Generated path escapes the library root")
            sidecars = tuple(
                (sidecar, destination.with_suffix(extension))
                for extension in SIDECAR_EXTENSIONS
                if (sidecar := path.with_suffix(extension)).is_file()
                and not sidecar.is_symlink()
            )
            media_id = (
                str(record["media_id"])
                if record is not None
                else (str(fields["title_id"]) if fields.get("title_id") else None)
            )
            moves.append(
                OrganizeMove(path, destination, sidecars, media_type, media_id)
            )
        except (OSError, ValueError, TypeError, sqlite3.Error) as error:
            counts["errors"] += 1
            problems.append(f"Cannot inspect {path}: {error}")
    # Two different sources may resolve to the same target. Skip *both*, and
    # preserve existing occupied names, including symlinks and stale records.
    destinations: Counter[Path] = Counter()
    for move in moves:
        destinations.update(
            [move.destination, *(target for _, target in move.sidecars)]
        )
    accepted: list[OrganizeMove] = []
    for move in moves:
        targets = [move.destination, *(target for _, target in move.sidecars)]
        if any(
            _exists(target)
            or destinations[target] > 1
            or not _safe_parents(root, target)
            for target in targets
        ):
            counts["collisions"] += 1
            problems.append(
                f"Skipping occupied or conflicting destination for {move.source}"
            )
            continue
        accepted.append(move)
    counts["planned"] = len(accepted)
    return OrganizePlan(root, accepted, counts, playlists, problems)


def _link_move(root: Path, files: list[tuple[Path, Path]]) -> None:
    """Publish all destinations exclusively before removing any source."""
    linked: list[Path] = []
    try:
        for source, destination in files:
            if not _safe_parents(root, destination):
                raise OSError(f"Destination uses a symlink directory: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink() or not source.is_file():
                raise OSError(f"Media source changed during organization: {source}")
            os.link(source, destination, follow_symlinks=False)
            linked.append(destination)
    except BaseException:
        for destination in reversed(linked):
            destination.unlink(missing_ok=True)
        raise


def _update_playlists(
    playlists: tuple[Path, ...],
    replacements: dict[Path, Path],
    problems: list[str],
) -> tuple[int, int]:
    updated = errors = 0
    for playlist in playlists:
        try:
            original = playlist.read_text(encoding="utf-8-sig")
            lines = original.splitlines(keepends=True)
            if not lines or lines[0].rstrip("\r\n") != "#EXTM3U":
                raise ValueError("Invalid M3U playlist header")
            changes = False
            for i, line in enumerate(lines):
                value = line.rstrip("\r\n")
                if not value or value.startswith("#"):
                    continue
                old = Path(value)
                if not old.is_absolute():
                    old = playlist.parent / old
                # M3U entries normally contain ../ components. Path.absolute()
                # does not collapse those, so normalize without following links.
                replacement = replacements.get(Path(os.path.abspath(old)))
                if replacement is None:
                    continue
                ending = line[len(value) :]
                lines[i] = (
                    Path(os.path.relpath(replacement, playlist.parent)).as_posix()
                    + ending
                )
                changes = True
            if changes:
                publish_bytes(
                    "".join(lines).encode("utf-8"), playlist, overwrite="replace"
                )
                updated += 1
        except (OSError, UnicodeError, ValueError) as error:
            errors += 1
            problems.append(f"Cannot update playlist {playlist}: {error}")
    return updated, errors


def execute_organization(
    plan: OrganizePlan,
    database: MediaDatabase | None = None,
    *,
    update_index: bool = False,
    on_move: Callable[[], None] | None = None,
) -> Counter[str]:
    """Apply collision-safe moves and repair relative playlist references."""
    counts = plan.counts.copy()
    replacements: dict[Path, Path] = {}
    parents: set[Path] = set()
    for move in plan.moves:
        files = [(move.source, move.destination), *move.sidecars]
        try:
            # New files are linked atomically with exclusive creation. If the
            # filesystem changes after planning, nothing is overwritten.
            _link_move(plan.root, files)
            try:
                relocated = (
                    database.relocate(move.source, move.destination)
                    if database is not None
                    else False
                )
                if (
                    not relocated
                    and update_index
                    and database is not None
                    and move.media_id is not None
                    and move.media_id.isdigit()
                ):
                    if database.get(move.destination) is not None:
                        raise FileExistsError(
                            f"Media index already contains {move.destination}"
                        )
                    database.index_file(
                        move.destination, move.media_type, move.media_id
                    )
            except (OSError, sqlite3.Error):
                for _, target in reversed(files):
                    target.unlink(missing_ok=True)
                raise
            for source, destination in files:
                source.unlink()
                parents.add(source.parent)
            replacements[move.source] = move.destination
            counts["moved"] += 1
        except (OSError, ValueError, sqlite3.Error) as error:
            counts["errors"] += 1
            plan.problems.append(f"Cannot move {move.source}: {error}")
        finally:
            if on_move is not None:
                on_move()

    playlist_updates, playlist_errors = _update_playlists(
        plan.playlists, replacements, plan.problems
    )
    counts["playlists_updated"] = playlist_updates
    counts["errors"] += playlist_errors
    # Remove only parent directories vacated by moved files, not unrelated
    # empty directories or the configured root itself.
    for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
        while parent != plan.root and parent.is_relative_to(plan.root):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    return counts
