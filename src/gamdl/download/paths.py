"""Pure filename/folder formatting with GAMDL 3.x template defaults."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gamdl.media import Media
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
    """Generate safe ordinary ``Path`` values without touching the filesystem.

    Defaults preserve GAMDL 3.x's folder and filename templates. A *directory*
    component that starts with '.' is prefixed with '_' instead, so a catalog
    title like '.Secret' cannot create hidden Unix directories. Output paths
    always stay beneath ``output_dir`` (template values never create folders).
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
        if not directory:
            # Never allow a generated filename to be '.' or '..'.
            if cleaned in {".", ".."}:
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

    def get_playlist_path(self, playlist: PlaylistTags) -> Path:
        """Format the same playlist folder and `.m3u8` filename as GAMDL 3.x."""
        from .tags import Tags

        return self._path(
            self.playlist_folder_template,
            self.playlist_file_template,
            self._fields(Tags(), playlist),
            ".m3u8",
        )
