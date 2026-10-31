"""Offline library relocation using the same metadata and paths as downloads.

No Apple Music requests, transcoding, tag writes or overwrites. Destination
collisions deliberately remain untouched instead of guessing which copy wins.
"""

from __future__ import annotations

import os
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from heapq import heappop, heappush
from pathlib import Path

from gamdl import _native

from .database import MediaDatabase
from .paths import PathFormatter
from .playlist import playlist_tags_from_header, split_playlist
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
    playlist_destinations: dict[Path, Path]
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
    playlist_destinations: dict[Path, Path] = {}
    proposed_playlists: dict[Path, Path] = {}
    for playlist in playlists:
        try:
            header, _ = split_playlist(
                playlist.read_text(encoding="utf-8-sig").splitlines()
            )
            metadata = playlist_tags_from_header(header)
            # Without both fields, a playlist's name and curator cannot be
            # recovered reliably. Such playlists can still have paths fixed.
            if not metadata.title or not metadata.artist:
                continue
            destination = formatter.get_playlist_path(metadata).absolute()
            if destination == playlist:
                continue
            if not destination.is_relative_to(root):
                raise ValueError("Generated playlist path escapes the library root")
            proposed_playlists[playlist] = destination
        except (OSError, UnicodeError, ValueError, TypeError) as error:
            counts["errors"] += 1
            problems.append(f"Cannot inspect playlist {playlist}: {error}")

    requested = Counter(proposed_playlists.values())
    for source, target in proposed_playlists.items():
        if _exists(target) or requested[target] > 1 or not _safe_parents(root, target):
            counts["collisions"] += 1
            problems.append(
                f"Skipping occupied or unsafe playlist destination for {source}"
            )
        else:
            playlist_destinations[source] = target
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
    return OrganizePlan(
        root, accepted, counts, playlists, playlist_destinations, problems
    )


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
    destinations: dict[Path, Path],
    root: Path,
    replacements: dict[Path, Path],
    problems: list[str],
) -> tuple[int, int]:
    updated = errors = 0
    for playlist in playlists:
        try:
            destination = destinations.get(playlist, playlist)
            original = playlist.read_text(encoding="utf-8-sig")
            lines = original.splitlines(keepends=True)
            if not lines or lines[0].rstrip("\r\n") != "#EXTM3U":
                raise ValueError("Invalid M3U playlist header")
            changes = destination != playlist
            for i, line in enumerate(lines):
                value = line.rstrip("\r\n")
                if not value or value.startswith("#") or "://" in value:
                    continue
                old = Path(value)
                if not old.is_absolute():
                    old = playlist.parent / old
                # M3U entries normally contain ../ components. Path.absolute()
                # does not collapse those, so normalize without following links.
                replacement = replacements.get(Path(os.path.abspath(old)), old)
                if destination == playlist and replacement == old:
                    continue
                ending = line[len(value) :]
                lines[i] = (
                    Path(os.path.relpath(replacement, destination.parent)).as_posix()
                    + ending
                )
                changes = True
            if changes:
                if not _safe_parents(root, destination):
                    raise OSError(f"Unsafe playlist destination: {destination}")
                written = publish_bytes(
                    "".join(lines).encode("utf-8"),
                    destination,
                    overwrite="skip" if destination != playlist else "replace",
                )
                if not written:
                    raise FileExistsError(
                        f"Playlist destination occupied: {destination}"
                    )
                if destination != playlist:
                    playlist.unlink()
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

    playlist_updates, playlist_errors = _update_playlists(
        plan.playlists,
        plan.playlist_destinations,
        plan.root,
        replacements,
        plan.problems,
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
