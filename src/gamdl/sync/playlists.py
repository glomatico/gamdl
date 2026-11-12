"""Plan and atomically publish complete Apple Music playlist snapshots.

Only playlist entries change. Never delete or overwrite local media, and
never replace a playlist from an incomplete remote response or failed download.
"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gamdl import _native
from gamdl.download.database import MediaDatabase
from gamdl.download.playlist import playlist_tags_from_header, split_playlist
from gamdl.download.publication import publish_bytes

_MEDIA_SUFFIXES = frozenset({".m4a", ".mp3", ".m4v", ".mp4"})
_PLAYLIST_ID = re.compile(r"(?:pl\.(?:[0-9a-z]{32}|u-[A-Za-z0-9]+)|p\.[A-Za-z0-9]+)\Z")


class PlaylistSyncError(ValueError):
    """An unsafe, incomplete or unsupported playlist cannot be synchronized."""


@dataclass(frozen=True)
class Track:
    """One remote playlist occurrence (duplicates are kept)."""

    kind: str
    media_id: str
    resource: dict[str, Any]

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.media_id)


@dataclass(frozen=True)
class Entry:
    """One existing playlist entry and its optional M3U comment lines."""

    path: Path
    comments: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlaylistSnapshot:
    path: Path
    playlist_id: str
    original: bytes
    header: tuple[str, ...]
    entries: tuple[Entry, ...]
    ending: str
    bom: bytes

    @classmethod
    def read(cls, path: Path, root: Path) -> PlaylistSnapshot:
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(root.resolve())
        ):
            raise PlaylistSyncError(
                "Playlist must be a regular file within the library"
            )
        data = path.read_bytes()
        bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
        try:
            text = data.decode("utf-8-sig")
        except UnicodeError as error:
            raise PlaylistSyncError("Playlist is not UTF-8") from error
        lines = text.splitlines()
        try:
            header, body = split_playlist(lines)
        except ValueError as error:
            raise PlaylistSyncError(str(error)) from error
        tags = playlist_tags_from_header(header)
        if not tags.playlist_id:
            raise PlaylistSyncError("Missing #Gamdl-APPLE-MUSIC-ID header")
        if _PLAYLIST_ID.fullmatch(tags.playlist_id) is None:
            raise PlaylistSyncError("Unsupported Apple Music playlist ID")
        pending: list[str] = []
        entries: list[Entry] = []
        for line in body:
            if line.startswith("#"):
                pending.append(line)
                continue
            if not line.strip():
                # Legacy downloads may leave blank position placeholders.
                continue
            if "://" in line:
                raise PlaylistSyncError("Playlist contains nonlocal URL entries")
            raw = Path(line)
            resolved = Path(
                os.path.abspath(raw if raw.is_absolute() else path.parent / raw)
            )
            if not resolved.resolve().is_relative_to(root.resolve()):
                raise PlaylistSyncError(
                    "Playlist references a file outside the library"
                )
            if resolved.is_symlink() or not resolved.is_file():
                raise PlaylistSyncError(f"Missing or unsafe local media: {line}")
            entries.append(Entry(resolved, tuple(pending)))
            pending.clear()
        if pending:
            raise PlaylistSyncError("Playlist contains unassociated trailing comments")
        ending = "\r\n" if b"\r\n" in data else "\n"
        return cls(
            path, tags.playlist_id, data, tuple(header), tuple(entries), ending, bom
        )


def remote_track(resource: dict[str, Any]) -> Track:
    """Extract a playable identity or fail rather than silently drop entries."""
    if not isinstance(resource, dict):
        raise PlaylistSyncError("Apple Music returned an invalid playlist track")
    kind = resource.get("type")
    kinds = {
        "songs": "songs",
        "song": "songs",
        "library-songs": "songs",
        "music-videos": "music-videos",
        "music-video": "music-videos",
        "library-music-videos": "music-videos",
    }
    if kind not in kinds:
        raise PlaylistSyncError(f"Unsupported track type in playlist: {kind}")
    media_id = resource.get("id")
    if kind.startswith("library-"):
        attributes = resource.get("attributes") or {}
        params = attributes.get("playParams") or {}
        media_id = params.get("catalogId") or media_id
    if not isinstance(media_id, str) or not media_id:
        raise PlaylistSyncError("Playlist track is missing a media ID")
    return Track(kinds[kind], media_id, resource)


class LocalMediaIndex:
    """Find local files by embedded catalog ID; index each library once.

    Use SQLite records when their size still matches. Unindexed and stale
    paths fall back to Gamdl's native MP4/MP3 tag readers, without network IO.
    """

    def __init__(self, root: Path, database: Path | None) -> None:
        self.root = root.absolute()
        self.paths: dict[tuple[str, str], list[Path]] = defaultdict(list)
        self.by_path: dict[Path, tuple[str, str]] = {}
        known: dict[Path, tuple[str, str, int]] = {}
        if database is not None and database.is_file():
            for record in MediaDatabase(database, root=self.root).local_records(
                self.root
            ):
                kind = record["media_type"]
                if kind in {"songs", "library-songs", "music-videos"}:
                    known[Path(record["path"])] = (
                        "music-videos" if kind == "music-videos" else "songs",
                        str(record["media_id"]),
                        record["file_size"],
                    )
        for path in self.root.rglob("*"):
            if path.suffix.lower() not in _MEDIA_SUFFIXES or path.is_symlink():
                continue
            if not path.is_file():
                continue
            record = known.get(path)
            if record is not None and path.stat().st_size == record[2]:
                kind, media_id = record[:2]
            else:
                try:
                    data = (
                        _native.read_mp3_organize_tags(str(path))
                        if path.suffix.lower() == ".mp3"
                        else _native.read_mp4_organize_tags(str(path))
                    )
                except (OSError, ValueError, TypeError):
                    continue
                kind = (
                    "music-videos"
                    if path.suffix.lower() == ".m4v" or data.get("media_type") == 6
                    else "songs"
                )
                media_id = data.get("title_id")
            if media_id:
                self.add(kind, str(media_id), path)

    def add(self, kind: str, media_id: str, path: Path) -> None:
        if (
            path.resolve().is_relative_to(self.root.resolve())
            and path.is_file()
            and not path.is_symlink()
            and path not in self.paths[(kind, media_id)]
        ):
            self.paths[(kind, media_id)].append(path)
            self.by_path[path] = (kind, media_id)

    def find(
        self, key: tuple[str, str], *, preferred: Path | None = None
    ) -> Path | None:
        matches = self.paths.get(key, ())
        if preferred in matches and preferred is not None and preferred.is_file():
            return preferred
        return next((p for p in matches if p.is_file() and not p.is_symlink()), None)

    def identity(self, path: Path) -> tuple[str, str] | None:
        return self.by_path.get(path)


@dataclass(frozen=True)
class PlaylistPlan:
    snapshot: PlaylistSnapshot
    tracks: tuple[Track, ...]
    added: int
    removed: int
    reordered: bool
    missing: tuple[Track, ...]


def plan_playlist(
    snapshot: PlaylistSnapshot, tracks: tuple[Track, ...], index: LocalMediaIndex
) -> PlaylistPlan:
    if not tracks and snapshot.entries:
        raise PlaylistSyncError(
            "Refusing to replace a non-empty playlist with an empty API response"
        )
    old_keys = [index.identity(entry.path) for entry in snapshot.entries]
    if any(key is None for key in old_keys):
        raise PlaylistSyncError("An existing playlist track has no readable media ID")
    current = Counter(old_keys)
    remote = Counter(item.key for item in tracks)
    added = sum((remote - current).values())
    removed = sum((current - remote).values())
    missing = tuple(
        {track.key: track for track in tracks if index.find(track.key) is None}.values()
    )
    return PlaylistPlan(
        snapshot,
        tracks,
        added,
        removed,
        old_keys != [track.key for track in tracks],
        missing,
    )


def render_playlist(plan: PlaylistPlan, index: LocalMediaIndex) -> bytes:
    """Preserve header, comments on retained entries, duplicate IDs and order."""
    old_entries: dict[tuple[str, str], deque[Entry]] = defaultdict(deque)
    for entry in plan.snapshot.entries:
        key = index.identity(entry.path)
        if key is None:
            raise PlaylistSyncError(f"Local media has no catalog ID: {entry.path}")
        old_entries[key].append(entry)
    output: list[str] = ["#EXTM3U", *plan.snapshot.header]
    for track in plan.tracks:
        prior = old_entries[track.key].popleft() if old_entries[track.key] else None
        target = index.find(track.key, preferred=prior.path if prior else None)
        if target is None:
            raise PlaylistSyncError(
                f"No downloaded file for {track.kind} {track.media_id}"
            )
        if prior is not None:
            output.extend(prior.comments)
        output.append(
            Path(os.path.relpath(target, plan.snapshot.path.parent)).as_posix()
        )
    content = plan.snapshot.ending.join(output) + plan.snapshot.ending
    return plan.snapshot.bom + content.encode("utf-8")


def publish_playlist(plan: PlaylistPlan, index: LocalMediaIndex) -> bool:
    """Replace only when the source has not changed since it was inspected."""
    rendered = render_playlist(plan, index)
    if rendered == plan.snapshot.original:
        return False
    if plan.snapshot.path.read_bytes() != plan.snapshot.original:
        raise PlaylistSyncError("Playlist was edited during synchronization")
    publish_bytes(rendered, plan.snapshot.path, overwrite="replace")
    return True
