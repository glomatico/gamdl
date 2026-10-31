"""Gamdl 3.x-compatible path formatting and filename collision policies."""

from __future__ import annotations

import os
import re
from collections.abc import Collection
from dataclasses import dataclass
from itertools import chain, count
from pathlib import Path
from string import Formatter
from typing import TYPE_CHECKING

from gamdl import _native

if TYPE_CHECKING:
    from gamdl.media import Media

    from .database import MediaDatabase
    from .tags import Tags


_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


class _LegacyFormatter(Formatter):
    def format_field(self, value: object, format_spec: str) -> str:
        if isinstance(value, tuple) and len(value) == 2:
            actual, fallback = value
            if actual is None or actual == "":
                return str(fallback)
            try:
                return super().format_field(actual, format_spec)
            except (ValueError, TypeError, OverflowError):
                return str(fallback)
        return super().format_field(value, format_spec)


_FORMATTER = _LegacyFormatter()


@dataclass(frozen=True)
class PlaylistTags:
    title: str | None = None
    artist: str | None = None
    playlist_id: str | None = None
    track: int | None = None


class PathFormatter:
    """Format paths and resolve existing-file collisions in one place.

    Defaults preserve Gamdl 3.x's folder and filename templates. A *directory*
    component that starts with '.' is prefixed with '_' instead, so a catalog
    title like '.Secret' cannot create hidden Unix directories. Output paths
    always stay beneath ``output_dir`` (template values never create folders).
    ``get_*_path`` only formats names; ``resolve_*_path`` inspects occupancy
    without changing the filesystem.
    """

    def __init__(
        self,
        output_dir: str | Path = "./Apple Music",
        *,
        album_folder_template: str = "{album_artist}/{album}",
        compilation_folder_template: str = "Compilations/{album}",
        no_album_folder_template: str = "{artist}/Unknown Album",
        playlist_folder_template: str = "Playlists/{playlist_artist}",
        single_disc_file_template: str = "{track:02d} {title}",
        multi_disc_file_template: str = "{disc}-{track:02d} {title}",
        no_album_file_template: str = "{title}",
        playlist_file_template: str = "{playlist_title}",
        truncate: int | None = None,
    ) -> None:
        if truncate is not None and (type(truncate) is not int or truncate < 1):
            raise ValueError("truncate must be a positive integer or None")
        self.output_dir = Path(output_dir)
        self.album_folder_template = album_folder_template
        self.compilation_folder_template = compilation_folder_template
        self.no_album_folder_template = no_album_folder_template
        self.playlist_folder_template = playlist_folder_template
        self.single_disc_file_template = single_disc_file_template
        self.multi_disc_file_template = multi_disc_file_template
        self.no_album_file_template = no_album_file_template
        self.playlist_file_template = playlist_file_template
        self.truncate = truncate

    @staticmethod
    def _render(template: str, values: dict[str, object]) -> str:
        # Disallow formatter attribute/index access and nested format specs.
        # Every interpolation is a flat, explicitly approved metadata field.
        for _, field, spec, conversion in _FORMATTER.parse(template):
            if field is not None and (
                field not in values
                or conversion is not None
                or "{" in spec
                or "}" in spec
            ):
                raise ValueError("Unsupported path template field")
        try:
            return _FORMATTER.vformat(template, (), values)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise ValueError("Invalid path template or metadata") from exc

    def _component(self, value: str, *, directory: bool, extension: str = "") -> str:
        cleaned = _INVALID.sub("_", value).strip()
        if directory and cleaned.startswith("."):
            cleaned = "_" + cleaned[1:]
        # Never allow a generated filename to be '.' or '..'.
        if not directory and cleaned in {".", ".."}:
            cleaned = cleaned.replace(".", "_")
        if cleaned.endswith("."):
            cleaned = cleaned[:-1] + "_"
        if not cleaned:
            cleaned = "_"
        # Restrict UTF-8 encoded length, not just number of Unicode points;
        # most Unix filesystems allow at most 255 bytes per component.
        limit = min(self.truncate or 255, 255) - len(extension.encode("utf-8"))
        if limit < 1:
            raise ValueError("File extension is too long")
        while len(cleaned.encode("utf-8")) > limit:
            cleaned = cleaned[:-1]
        if not cleaned or cleaned in {".", ".."}:
            cleaned = "_"
        if directory and cleaned.startswith("."):
            cleaned = "_" + cleaned[1:]
        return cleaned + extension

    def _path(
        self,
        folder_template: str,
        file_template: str,
        fields: dict[str, object],
        extension: str,
    ) -> Path:
        if not extension.startswith(".") or "/" in extension or "\\" in extension:
            raise ValueError("Invalid file extension")
        parts: list[str] = []
        for is_file_template, template in (
            (False, folder_template),
            (True, file_template),
        ):
            segments = template.split("/")
            if not all(segments):
                raise ValueError("Templates must not contain empty path components")
            for i, segment in enumerate(segments):
                file_part = is_file_template and i == len(segments) - 1
                # Delimiter removal occurs *after* formatting so values can
                # never escape a component or manufacture a path separator.
                formatted = self._render(segment, fields)
                parts.append(
                    self._component(
                        formatted,
                        directory=not file_part,
                        extension=extension if file_part else "",
                    )
                )
        return self.output_dir.joinpath(*parts)

    @staticmethod
    def _fields(tags: Tags, playlist: PlaylistTags | None) -> dict[str, object]:
        def fallback(value: object | None, default: str) -> object:
            return (value, default)

        return {
            "album": fallback(tags.album, "Unknown Album"),
            "album_artist": fallback(tags.album_artist, "Unknown Artist"),
            "album_id": fallback(tags.album_id, "Unknown Album ID"),
            "artist": fallback(tags.artist, "Unknown Artist"),
            "artist_id": fallback(tags.artist_id, "Unknown Artist ID"),
            "composer": fallback(tags.composer, "Unknown Composer"),
            "composer_id": fallback(tags.composer_id, "Unknown Composer ID"),
            "date": fallback(tags.release_date, "Unknown Date"),
            "disc": fallback(tags.disc_number, ""),
            "disc_total": fallback(tags.disc_total, ""),
            "media_type": fallback(tags.media_type, "Unknown Media Type"),
            "playlist_artist": fallback(
                playlist.artist if playlist else None, "Unknown Playlist Artist"
            ),
            "playlist_id": fallback(
                playlist.playlist_id if playlist else None, "Unknown Playlist ID"
            ),
            "playlist_title": fallback(
                playlist.title if playlist else None, "Unknown Playlist Title"
            ),
            "playlist_track": fallback(playlist.track if playlist else None, ""),
            "title": fallback(tags.title, "Unknown Title"),
            "title_id": fallback(tags.title_id, "Unknown Title ID"),
            "track": fallback(tags.track_number, ""),
            "track_total": fallback(tags.track_total, ""),
        }

    def get_media_path(
        self,
        media: Media,
        tags: Tags,
        *,
        extension: str,
        playlist: PlaylistTags | None = None,
    ) -> Path:
        """Format an output media path using legacy folder/file decisions."""
        if tags.album:
            folder = (
                self.compilation_folder_template
                if tags.compilation
                else self.album_folder_template
            )
            file = (
                self.multi_disc_file_template
                if tags.disc_total is not None and tags.disc_total > 1
                else self.single_disc_file_template
            )
        else:
            folder = self.no_album_folder_template
            file = self.no_album_file_template
        return self._path(folder, file, self._fields(tags, playlist), extension)

    @staticmethod
    def numbered_video_path(path: Path, index: int) -> Path:
        """Gamdl 3.x's ``Title 2.m4v`` naming, with UTF-8 byte limits."""
        if index < 1:
            raise ValueError("Video path index must be positive")
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

    @classmethod
    def video_index(cls, base: Path, candidate: Path) -> int | None:
        """Recognize only canonical numbered variants of ``base``."""
        if candidate.name == base.name:
            return 1
        if candidate.suffix != base.suffix:
            return None
        # Never treat "Title 1" or "Title 02" as automatically numbered.
        match = re.search(r" ((?:[2-9]|[1-9][0-9]+))$", candidate.stem)
        if match is None or len(match.group(1)) > 12:
            return None
        index = int(match.group(1))
        return (
            index
            if cls.numbered_video_path(base, index).name == candidate.name
            else None
        )

    @staticmethod
    def _matches_media_id(
        path: Path,
        media: Media,
        *,
        database: MediaDatabase | None,
        catalog_id: str | None,
    ) -> bool:
        """Use database identity first, then the native MP4 catalog ID."""
        if path.is_symlink() or not path.is_file():
            return False
        record = database.get(path) if database is not None else None
        if record is not None:
            return record["media_id"] == media.id and record["media_type"] == media.type
        return bool(catalog_id) and _native.read_mp4_title_id(str(path)) == catalog_id

    def resolve_video_path(
        self,
        path: Path,
        media: Media | None = None,
        *,
        database: MediaDatabase | None = None,
        preferred: Path | None = None,
        reserved: Collection[Path] = (),
        releasing: Collection[Path] = (),
        sidecar_extensions: Collection[str] = (),
    ) -> Path:
        """Reuse a matching video or choose the lowest free numbered slot.

        The downloader supplies ``media`` to identify an already downloaded
        video. The organizer supplies ``preferred`` (its current filename),
        pending ``reserved`` targets and sidecar extensions instead. Paths
        being relocated can be listed in ``releasing`` so they may be reused
        *after* their owning moves succeed. The organizer must enforce that
        ordering, safe directories and no-overwrite publication.
        """
        existing: dict[int, Path] = {}
        if path.parent.exists():
            for candidate in path.parent.iterdir():
                index = self.video_index(path, candidate)
                if index is not None:
                    existing[index] = candidate

        if media is not None:
            for _, candidate in sorted(existing.items()):
                if (
                    candidate not in reserved
                    and candidate not in releasing
                    and self._matches_media_id(
                        candidate, media, database=database, catalog_id=media.id
                    )
                ):
                    return candidate

        preferred_index = self.video_index(path, preferred) if preferred else None
        indices = chain(
            (preferred_index,) if preferred_index is not None else (), count(1)
        )
        for index in indices:
            if index in existing and existing[index] not in releasing:
                continue
            destination = self.numbered_video_path(path, index)
            targets = (
                destination,
                *(destination.with_suffix(ext) for ext in sidecar_extensions),
            )
            if any(
                target in reserved
                or (os.path.lexists(target) and target not in releasing)
                for target in targets
            ):
                continue
            return destination

    def get_playlist_path(self, playlist: PlaylistTags) -> Path:
        """Format the same playlist folder and `.m3u8` filename as Gamdl 3.x."""
        from .tags import Tags

        return self._path(
            self.playlist_folder_template,
            self.playlist_file_template,
            self._fields(Tags(), playlist),
            ".m3u8",
        )
