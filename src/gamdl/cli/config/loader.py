"""Validated YAML configuration and native Click default maps."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import yaml

from gamdl.download import PathFormatter, PlaylistTags, Tags
from gamdl.media import CoverFormat, MusicVideoCodec, SongCodec, UploadedVideoQuality
from gamdl.media.constants import ARTIST_AUTO_SELECT_NAMES, LIBRARY_AUTO_SELECT_NAMES

from .models import (
    AppConfig,
    AuthConfig,
    DatabaseConfig,
    DownloadConfig,
    OutputConfig,
    UiConfig,
)


class ConfigError(ValueError):
    """Invalid YAML syntax, key, type, or value."""


class _UniqueLoader(yaml.SafeLoader):
    """Reject ambiguous repeated keys instead of silently discarding settings."""


def _mapping(loader: _UniqueLoader, node: yaml.MappingNode) -> dict:
    result: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str):
            raise ConfigError("All YAML setting names must be strings")
        if key in result:
            raise ConfigError(f"Duplicate YAML setting: {key}")
        result[key] = loader.construct_object(value_node)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _merge(defaults: dict[str, Any], values: dict[str, Any], prefix: str = "") -> None:
    for key, value in values.items():
        name = f"{prefix}.{key}" if prefix else key
        if key not in defaults:
            continue
        if isinstance(defaults[key], dict):
            if not isinstance(value, dict):
                raise ConfigError(f"{name} must be a mapping")
            _merge(defaults[key], value, name)
        else:
            defaults[key] = value


def _validate(config: dict[str, Any]) -> AppConfig:
    if type(config["config_version"]) is not int or config["config_version"] != 1:
        raise ConfigError("config_version must be 1")
    groups = {
        "auth": AuthConfig,
        "download": DownloadConfig,
        "output": OutputConfig,
        "database": DatabaseConfig,
        "ui": UiConfig,
    }
    for section, cls in groups.items():
        defaults = asdict(cls())
        for key, value in config[section].items():
            hint = defaults[key]
            path = f"{section}.{key}"
            if hint is None:
                if value is not None and (
                    not isinstance(value, str) or not value.strip()
                ):
                    raise ConfigError(f"{path} must be a string or null")
            elif isinstance(hint, bool):
                if type(value) is not bool:
                    raise ConfigError(f"{path} must be a boolean")
            elif isinstance(hint, int):
                if type(value) is not int or value < 1:
                    raise ConfigError(f"{path} must be a positive integer")
            elif isinstance(hint, list):
                if (
                    not isinstance(value, list)
                    or (
                        not value
                        and path
                        not in {
                            "download.artist_auto_select",
                            "download.library_auto_select",
                        }
                    )
                    or any(
                        not isinstance(item, str) or not item.strip() for item in value
                    )
                ):
                    raise ConfigError(
                        f"{path} must be a list of strings"
                        if path
                        in {
                            "download.artist_auto_select",
                            "download.library_auto_select",
                        }
                        else f"{path} must be a nonempty list of strings"
                    )
            elif not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{path} must be a nonempty string")

    auth = AuthConfig(**config["auth"])
    download = DownloadConfig(**config["download"])
    output = OutputConfig(**config["output"])
    database = DatabaseConfig(**config["database"])
    ui = UiConfig(**config["ui"])
    if auth.mode not in {None, "cookies", "wrapper", "media-user-token"}:
        raise ConfigError(
            "auth.mode must be null, cookies, wrapper or media-user-token"
        )
    if auth.drm_backend not in {"widevine", "playready"}:
        raise ConfigError("auth.drm_backend must be widevine or playready")
    # The CLI can ask for a missing PlayReady .prd interactively. In CI, the
    # same missing path is reported without attempting to read from stdin.
    if auth.storefront is not None and (
        len(auth.storefront) != 2
        or not auth.storefront.isascii()
        or not auth.storefront.isalpha()
    ):
        raise ConfigError("auth.storefront must be a two-letter country code")
    if download.overwrite not in {"skip", "replace", "error"}:
        raise ConfigError("download.overwrite must be skip, replace, or error")
    if output.cover_format not in {item.value for item in CoverFormat}:
        raise ConfigError("output.cover_format must be jpg or png")
    for name, values, allowed in (
        ("artist_auto_select", download.artist_auto_select, ARTIST_AUTO_SELECT_NAMES),
        (
            "library_auto_select",
            download.library_auto_select,
            LIBRARY_AUTO_SELECT_NAMES,
        ),
    ):
        if any(value not in allowed and value != "all" for value in values):
            raise ConfigError(f"Invalid download.{name}")
        if len(values) != len(set(values)):
            raise ConfigError(f"download.{name} must not contain duplicates")
    if download.song_codec is not None and download.song_codec not in {
        c.value for c in SongCodec
    }:
        raise ConfigError("Invalid download.song_codec")
    if any(c not in {t.value for t in SongCodec} for c in download.song_codec_priority):
        raise ConfigError("Invalid download.song_codec_priority")
    if download.music_video_codec is not None and download.music_video_codec not in {
        c.value for c in MusicVideoCodec
    }:
        raise ConfigError("Invalid download.music_video_codec")
    if any(
        c not in {t.value for t in MusicVideoCodec}
        for c in download.music_video_codec_priority
    ):
        raise ConfigError("Invalid download.music_video_codec_priority")
    if (
        download.uploaded_video_quality is not None
        and download.uploaded_video_quality
        not in {c.value for c in UploadedVideoQuality}
    ):
        raise ConfigError("Invalid download.uploaded_video_quality")
    if output.truncate is not None and (
        type(output.truncate) is not int or output.truncate < 1
    ):
        raise ConfigError("output.truncate must be a positive integer or null")
    formatter = PathFormatter(
        album_folder_template=output.album_folder_template,
        compilation_folder_template=output.compilation_folder_template,
        no_album_folder_template=output.no_album_folder_template,
        playlist_folder_template=output.playlist_folder_template,
        single_disc_file_template=output.single_disc_file_template,
        multi_disc_file_template=output.multi_disc_file_template,
        no_album_file_template=output.no_album_file_template,
        playlist_file_template=output.playlist_file_template,
        truncate=output.truncate,
    )
    song_tags = Tags(
        title="Example",
        artist="Artist",
        album_artist="Artist",
        album="Album",
        track_number=1,
    )
    try:
        for tags in (
            song_tags,
            replace(song_tags, compilation=True),
            replace(song_tags, disc_total=2, disc_number=1),
            replace(song_tags, album=None),
        ):
            formatter.get_media_path(None, tags, extension=".m4a")
        formatter.get_playlist_path(
            PlaylistTags(title="List", artist="Curator", playlist_id="id", track=1)
        )
    except ValueError as exc:
        raise ConfigError(f"Invalid output template: {exc}") from exc
    return AppConfig(
        auth=auth, download=download, output=output, database=database, ui=ui
    )


def _normalize(values: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Drop unknown keys and populate missing keys without changing known values."""
    result = {}
    for key, default in defaults.items():
        value = values.get(key, default)
        if isinstance(default, dict) and isinstance(value, dict):
            value = _normalize(value, default)
        result[key] = value
    return result


def _persist(path: Path, normalized: dict[str, Any]) -> None:
    """Back up the original and atomically write a validated normalized config."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".bak")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    if backup.exists():
        backup.chmod(0o600)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        import os

        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            yaml.safe_dump(normalized, stream, sort_keys=False, allow_unicode=True)
        os.replace(name, path)
        path.chmod(0o600)
    finally:
        Path(name).unlink(missing_ok=True)


def save_auth_configuration(
    path: Path,
    *,
    mode: str,
    cookies_path: str,
    wrapper_url: str,
    media_user_token: str | None,
) -> None:
    """Persist a validated authentication choice without disturbing other settings."""
    if path.exists():
        try:
            loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ConfigError(
                "Cannot read configuration for authentication setup"
            ) from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError("YAML config must be a mapping")
    else:
        loaded = asdict(AppConfig())
    auth = loaded.setdefault("auth", {})
    auth.update(
        mode=mode,
        cookies_path=cookies_path,
        wrapper_url=wrapper_url,
        media_user_token=media_user_token,
    )
    defaults = asdict(AppConfig())
    _merge(defaults, loaded)
    _validate(defaults)
    try:
        _persist(path, _normalize(loaded, asdict(AppConfig())))
    except OSError as exc:
        raise ConfigError("Unable to save authentication configuration") from exc


def load_config(path: Path | None) -> AppConfig:
    """Load, validate and persist normalized YAML when the schema changes."""
    defaults = asdict(AppConfig())
    if path is None or not path.exists():
        return _validate(defaults)
    try:
        loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read config: {exc}") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ConfigError("YAML config must be a mapping")
    schema = asdict(AppConfig())
    normalized = _normalize(loaded, schema)
    _merge(defaults, loaded)
    result = _validate(defaults)
    if normalized != loaded:
        try:
            _persist(path, normalized)
        except OSError as exc:
            raise ConfigError(f"Cannot update config: {exc}") from exc
    return result


def click_default_map(config: AppConfig) -> dict[str, Any]:
    """Translate grouped settings to native Click rip option names."""
    a, d, o, b, ui = (
        config.auth,
        config.download,
        config.output,
        config.database,
        config.ui,
    )
    return {
        "auth_mode": a.mode,
        "cookies_path": a.cookies_path,
        "wrapper_url": a.wrapper_url,
        "media_user_token": a.media_user_token,
        "wrapper_decrypt_host": a.wrapper_decrypt_host,
        "wrapper_decrypt_port": a.wrapper_decrypt_port,
        "storefront": a.storefront,
        "language": a.language,
        "drm_backend": a.drm_backend,
        "device_path": a.device_path,
        "output_dir": o.directory,
        "temp_path": o.temp_path,
        "overwrite": d.overwrite,
        "concurrency": d.concurrency,
        "concurrent_downloads": d.concurrent_downloads,
        "song_codec": d.song_codec,
        "song_codec_priority": tuple(d.song_codec_priority),
        "music_video_codec": d.music_video_codec,
        "music_video_codec_priority": tuple(d.music_video_codec_priority),
        "max_height": d.max_height,
        "uploaded_video_quality": d.uploaded_video_quality,
        "save_cover": o.save_cover,
        "cover_format": o.cover_format,
        "no_lyrics": not o.save_lyrics,
        "save_playlist": o.save_playlist,
        "fail_fast": not d.continue_on_error,
        "artist_auto_select": tuple(d.artist_auto_select),
        "library_auto_select": tuple(d.library_auto_select),
        "album_folder_template": o.album_folder_template,
        "compilation_folder_template": o.compilation_folder_template,
        "no_album_folder_template": o.no_album_folder_template,
        "playlist_folder_template": o.playlist_folder_template,
        "single_disc_file_template": o.single_disc_file_template,
        "multi_disc_file_template": o.multi_disc_file_template,
        "no_album_file_template": o.no_album_file_template,
        "playlist_file_template": o.playlist_file_template,
        "truncate": o.truncate,
        "database": b.enabled,
        "database_path": b.path,
        "no_progress": not ui.progress,
        "max_visible_tracks": ui.max_visible_tracks,
        "debug": ui.debug,
    }
