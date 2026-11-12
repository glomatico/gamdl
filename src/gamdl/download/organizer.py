"""Offline library relocation using the same metadata and paths as downloads.

No Apple Music requests, transcoding, tag writes or overwrites. Destination
collisions deliberately remain untouched instead of guessing which copy wins.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from heapq import heappop, heappush
from pathlib import Path

from gamdl import _native

from .database import MediaDatabase
from .paths import PathFormatter
from .publication import publish_bytes
from .tags import Tags

MEDIA_EXTENSIONS = frozenset({".m4a", ".m4v", ".mp4", ".mp3"})
SIDECAR_EXTENSIONS = (".lrc", ".srt", ".ttml", ".txt", ".jpg", ".jpeg", ".png")
_TRACK_NUMBER = re.compile(r"^(?:\d{1,3}-)?\d{1,3}\s+")


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


def _targets(move: OrganizeMove) -> tuple[Path, ...]:
    return (move.destination, *(target for _, target in move.sidecars))


def _video_destination(move: OrganizeMove, path: Path) -> OrganizeMove:
    """Apply the download resolver's numbered-video policy to sidecars too."""
    return replace(
        move,
        destination=path,
        sidecars=tuple(
            (source, path.with_suffix(target.suffix))
            for source, target in move.sidecars
        ),
    )


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
    root: Path,
    formatter: PathFormatter,
    database: MediaDatabase | None = None,
    *,
    playlists_only: bool = False,
) -> OrganizePlan:
    """Inspect metadata and destinations before any files are moved."""
    root = root.absolute()
    counts: Counter[str] = Counter()
    moves: list[OrganizeMove] = []
    problems: list[str] = []
    # Playlist-only repairs need filenames, not expensive native media tag
    # parsing. The execution phase builds a lightweight path index instead.
    candidates = [] if playlists_only else sorted(_candidates(root))
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
            if path == destination or (
                media_type == "music-videos"
                and path.parent == destination.parent
                and formatter.video_index(destination, path) is not None
            ):
                # Numbered video names are already canonical. A valid
                # "Title 2.m4v" must not be collapsed onto "Title.m4v".
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
    # Songs retain their conservative collision policy. Videos instead use
    # the same "Title", "Title 2", "Title 3" slots as the rip command.
    # Reserve song destinations before assigning video slots, so a video's
    # artwork cannot overwrite a song's planned sidecar.
    songs = [move for move in moves if move.media_type != "music-videos"]
    videos = [move for move in moves if move.media_type == "music-videos"]
    destinations: Counter[Path] = Counter(
        target for move in songs for target in _targets(move)
    )
    accepted: list[OrganizeMove] = []
    for move in songs:
        targets = _targets(move)
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
    reserved = {target for move in accepted for target in _targets(move)}

    # Reusable source paths are not necessarily free *yet*. The executor
    # processes the moves that release them first, and never overwrites a
    # source if a preceding move fails. Do not count unsafe moves as releases.
    releasing = {
        source
        for move in (*accepted, *videos)
        if _safe_parents(root, move.destination)
        for source in (move.source, *(sidecar for sidecar, _ in move.sidecars))
    }

    # Prefer the video's existing number if its basename still matches the
    # current template, even when its containing folder has changed.
    # Sorting by requested slot gives unnumbered files priority over 2, 3, ...
    # when several videos are being moved to the same destination directory.
    videos.sort(
        key=lambda move: (
            str(move.destination),
            formatter.video_index(move.destination, move.source) or 1_000_001,
            str(move.source),
        )
    )
    for move in videos:
        if not _safe_parents(root, move.destination):
            counts["collisions"] += 1
            problems.append(f"Skipping unsafe destination for {move.source}")
            continue
        try:
            destination = formatter.resolve_video_path(
                move.destination,
                preferred=move.source,
                reserved=reserved,
                releasing=releasing,
                sidecar_extensions=tuple(target.suffix for _, target in move.sidecars),
            )
        except (OSError, ValueError) as error:
            counts["collisions"] += 1
            problems.append(
                f"Cannot allocate video destination for {move.source}: {error}"
            )
            continue
        candidate = _video_destination(move, destination)
        accepted.append(candidate)
        reserved.update(_targets(candidate))

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


def _reference_key(root: Path, path: Path) -> tuple[str, str, str, str] | None:
    """Conservative album/artist/title match across filename renumbering.

    Never guess across albums or artists; ambiguous versions are left alone.
    """
    if not path.is_relative_to(root) or path.suffix.lower() not in MEDIA_EXTENSIONS:
        return None
    components = path.relative_to(root).parts
    if len(components) < 3:
        return None
    name = _TRACK_NUMBER.sub("", path.stem).casefold()
    return (
        components[-3].casefold(),
        components[-2].casefold(),
        name,
        path.suffix.lower(),
    )


def _playlist_reference_index(
    root: Path,
) -> dict[tuple[str, str, str, str], Path | None]:
    """Index present files once for efficient, unambiguous reference repair."""
    index: dict[tuple[str, str, str, str], Path | None] = {}
    for path in _candidates(root):
        key = _reference_key(root, path)
        if key is not None:
            index[key] = None if key in index else path
    return index


def _update_playlists(
    playlists: tuple[Path, ...],
    root: Path,
    replacements: dict[Path, Path],
    problems: list[str],
    *,
    dry_run: bool = False,
) -> tuple[int, int, int, int]:
    """Rewrite *only* changed local-media path lines in existing playlists.

    Never create/rename playlist files or modify title, curator, catalog ID,
    EXTINF, other comments, or the ordering of entries.
    """
    updated = errors = repaired = unresolved = 0
    reference_index = _playlist_reference_index(root)
    for playlist in playlists:
        try:
            original_bytes = playlist.read_bytes()
            original = original_bytes.decode("utf-8-sig")
            lines = original.splitlines(keepends=True)
            if not lines or lines[0].rstrip("\r\n") != "#EXTM3U":
                raise ValueError("Invalid M3U playlist header")
            changes = False
            for i, line in enumerate(lines):
                value = line.rstrip("\r\n")
                if not value or value.startswith("#") or "://" in value:
                    continue
                old = Path(value)
                if not old.is_absolute():
                    old = playlist.parent / old
                # M3U entries normally contain ../ components. Path.absolute()
                # does not collapse those, so normalize without following links.
                normalized = Path(os.path.abspath(old))
                replacement = replacements.get(normalized, normalized)
                if not replacement.is_file() and not replacement.is_symlink():
                    key = _reference_key(root, normalized)
                    candidate = reference_index.get(key) if key is not None else None
                    if candidate is not None and candidate.is_file():
                        replacement = candidate
                        repaired += 1
                    elif normalized.is_relative_to(root):
                        unresolved += 1
                if replacement == normalized:
                    continue
                ending = line[len(value) :]
                lines[i] = (
                    Path(os.path.relpath(replacement, playlist.parent)).as_posix()
                    + ending
                )
                changes = True
            if changes:
                if not dry_run:
                    if not _safe_parents(root, playlist):
                        raise OSError(f"Unsafe playlist destination: {playlist}")
                    new_bytes = "".join(lines).encode("utf-8")
                    if original_bytes.startswith(b"\xef\xbb\xbf"):
                        new_bytes = b"\xef\xbb\xbf" + new_bytes
                    publish_bytes(new_bytes, playlist, overwrite="replace")
                updated += 1
        except (OSError, UnicodeError, ValueError) as error:
            errors += 1
            problems.append(f"Cannot update playlist {playlist}: {error}")
    return updated, errors, repaired, unresolved


def preview_playlist_repairs(plan: OrganizePlan) -> Counter[str]:
    """Report playlist-only edits without touching the library."""
    problems: list[str] = []
    updated, errors, repaired, unresolved = _update_playlists(
        plan.playlists, plan.root, {}, problems, dry_run=True
    )
    plan.problems.extend(problems)
    counts = plan.counts.copy()
    counts["playlists_updated"] = updated
    counts["playlist_references_repaired"] = repaired
    counts["playlist_references_unresolved"] = unresolved
    counts["errors"] += errors
    return counts


def execute_organization(
    plan: OrganizePlan,
    database: MediaDatabase | None = None,
    *,
    update_index: bool = False,
    playlists_only: bool = False,
    on_move: Callable[[], None] | None = None,
) -> Counter[str]:
    """Apply collision-safe moves and repair relative playlist references."""
    counts = plan.counts.copy()
    replacements: dict[Path, Path] = {}
    parents: set[Path] = set()

    # Repair playlists on an already-organized library without touching media
    # files or the SQLite index. Headers and playlist filenames are untouched.
    if playlists_only:
        updated, errors, repaired, unresolved = _update_playlists(
            plan.playlists, plan.root, {}, plan.problems
        )
        counts.update(
            moved=0,
            planned=0,
            playlists_updated=updated,
            playlist_references_repaired=repaired,
            playlist_references_unresolved=unresolved,
        )
        counts["errors"] += errors
        return counts

    # A destination may be another planned move's source. Free that source
    # before trying to publish its replacement, so the normal exclusive-link
    # guarantee remains in force. Cycles cannot be resolved without staging;
    # leave every file involved untouched rather than risk overwriting it.
    source_owner = {
        source: i
        for i, move in enumerate(plan.moves)
        for source in (move.source, *(path for path, _ in move.sidecars))
    }
    dependencies = [
        {
            owner
            for target in _targets(move)
            if (owner := source_owner.get(target)) is not None and owner != i
        }
        for i, move in enumerate(plan.moves)
    ]
    dependents: list[set[int]] = [set() for _ in plan.moves]
    for i, owners in enumerate(dependencies):
        for owner in owners:
            dependents[owner].add(i)
    ready = [i for i, owners in enumerate(dependencies) if not owners]
    ordered: list[int] = []
    while ready:
        i = heappop(ready)
        ordered.append(i)
        for other in dependents[i]:
            dependencies[other].remove(i)
            if not dependencies[other]:
                heappush(ready, other)

    for i in ordered:
        move = plan.moves[i]
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

    for i, dependencies_remaining in enumerate(dependencies):
        if dependencies_remaining:
            counts["collisions"] += 1
            plan.problems.append(
                f"Cannot move {plan.moves[i].source}: cyclic or blocked path dependency"
            )
            if on_move is not None:
                on_move()

    playlist_updates, playlist_errors, repaired, unresolved = _update_playlists(
        plan.playlists,
        plan.root,
        replacements,
        plan.problems,
    )
    counts["playlists_updated"] = playlist_updates
    counts["playlist_references_repaired"] = repaired
    counts["playlist_references_unresolved"] = unresolved
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
