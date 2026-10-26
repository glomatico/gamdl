"""Validated YAML configuration, profiles, and native Click default maps."""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import yaml

from gamdl.download import PathFormatter, PlaylistTags, Tags
from gamdl.media import MusicVideoCodec, SongCodec, UploadedVideoQuality

from .models import (
    AppConfig,
    AuthConfig,
    DatabaseConfig,
    DownloadConfig,
    OutputConfig,
    UiConfig,
)


class ConfigError(ValueError):
    """Invalid YAML syntax, key, type, value, or configuration profile."""


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
            raise ConfigError(f"Unknown configuration key: {name}")
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
                    or not value
                    or any(
                        not isinstance(item, str) or not item.strip() for item in value
                    )
                ):
                    raise ConfigError(f"{path} must be a nonempty list of strings")
            elif not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{path} must be a nonempty string")

    auth = AuthConfig(**config["auth"])
    download = DownloadConfig(**config["download"])
    output = OutputConfig(**config["output"])
    database = DatabaseConfig(**config["database"])
    ui = UiConfig(**config["ui"])
    if auth.mode not in {"cookies", "wrapper"}:
        raise ConfigError("auth.mode must be cookies or wrapper")
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


def load_config(path: Path | None, *, profile: str | None = None) -> AppConfig:
    """Load config and optional profile; never create, edit or normalize the YAML file."""
    defaults = asdict(AppConfig())
    if path is None or not path.exists():
        if profile:
            raise ConfigError(f"Profile {profile!r} requires a config file")
        return _validate(defaults)
    try:
        loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read config: {exc}") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ConfigError("YAML config must be a mapping")
    base = {key: value for key, value in loaded.items() if key != "profiles"}
    profiles = loaded.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ConfigError("profiles must be a mapping")
    _merge(defaults, base)
    _validate(defaults)
    for name, settings in profiles.items():
        if not isinstance(name, str) or not name or not isinstance(settings, dict):
            raise ConfigError("Each profile must have a name and mapping")
        copy = asdict(AppConfig())
        _merge(copy, base)
        _merge(copy, settings)
        _validate(copy)
    if profile:
        if profile not in profiles:
            raise ConfigError(f"Unknown profile: {profile}")
        _merge(defaults, profiles[profile])
    return _validate(defaults)


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
        "wrapper_decrypt_host": a.wrapper_decrypt_host,
        "wrapper_decrypt_port": a.wrapper_decrypt_port,
        "storefront": a.storefront,
        "language": a.language,
        "drm_backend": a.drm_backend,
        "device_path": a.device_path,
        "output_dir": o.directory,
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
        "save_lyrics": o.save_lyrics,
        "save_playlist": o.save_playlist,
        "continue_on_error": d.continue_on_error,
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
        "progress": ui.progress,
        "max_visible_tracks": ui.max_visible_tracks,
        "debug": ui.debug,
    }
