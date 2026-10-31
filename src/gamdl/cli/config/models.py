"""Typed effective configuration, independent of Click's option objects."""

from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_config_path, user_data_path


def config_path() -> Path:
    return user_config_path("gamdl", appauthor=False) / "config.yaml"


def database_path() -> Path:
    return user_data_path("gamdl", appauthor=False) / "media.sqlite3"


@dataclass(frozen=True)
class AuthConfig:
    mode: str | None = None
    cookies_path: str = str(user_config_path("gamdl", appauthor=False) / "cookies.txt")
    wrapper_url: str = "http://127.0.0.1"
    media_user_token: str | None = None
    wrapper_decrypt_host: str = "127.0.0.1"
    wrapper_decrypt_port: int = 10020
    storefront: str | None = None
    language: str = "en-US"
    drm_backend: str = "widevine"
    device_path: str | None = None


@dataclass(frozen=True)
class DownloadConfig:
    overwrite: str = "skip"
    concurrency: int = 8
    concurrent_downloads: int = 2
    song_codec: str | None = None
    song_codec_priority: list[str] = field(default_factory=lambda: ["aac-web"])
    music_video_codec: str | None = None
    music_video_codec_priority: list[str] = field(
        default_factory=lambda: ["h264", "h265"]
    )
    max_height: int = 1080
    uploaded_video_quality: str | None = None
    continue_on_error: bool = True
    artist_auto_select: list[str] = field(default_factory=list)
    library_auto_select: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OutputConfig:
    directory: str = "./Apple Music"
    temp_path: str | None = None
    tag: bool = True
    exclude_tags: list[str] = field(default_factory=list)
    save_cover: bool = False
    cover_format: str = "jpg"
    save_lyrics: bool = True
    save_playlist: bool = False
    album_folder_template: str = "{album_artist}/{album}"
    compilation_folder_template: str = "Compilations/{album}"
    no_album_folder_template: str = "{artist}/Unknown Album"
    playlist_folder_template: str = "Playlists/{playlist_artist}"
    single_disc_file_template: str = "{track:02d} {title}"
    multi_disc_file_template: str = "{disc}-{track:02d} {title}"
    no_album_file_template: str = "{title}"
    playlist_file_template: str = "{playlist_title}"
    truncate: int | None = None


@dataclass(frozen=True)
class DatabaseConfig:
    enabled: bool = False
    path: str = str(database_path())


@dataclass(frozen=True)
class UiConfig:
    progress: bool = True
    max_visible_tracks: int = 5
    debug: bool = False


@dataclass(frozen=True)
class AppConfig:
    config_version: int = 1
    auth: AuthConfig = field(default_factory=AuthConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    ui: UiConfig = field(default_factory=UiConfig)
