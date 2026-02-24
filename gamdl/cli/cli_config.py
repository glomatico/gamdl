import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import click
from dataclass_click import argument, option

from ..api import AppleMusicApi
from ..downloader import (
    AppleMusicBaseDownloader,
    AppleMusicMusicVideoDownloader,
    AppleMusicSongDownloader,
    AppleMusicUploadedVideoDownloader,
    DownloadMode,
    RemuxFormatMusicVideo,
    RemuxMode,
)
from ..interface import (
    CoverFormat,
    MusicVideoCodec,
    MusicVideoResolution,
    SongCodec,
    SyncedLyricsFormat,
    UploadedVideoQuality,
)
from .utils import Csv

api_from_cookies_sig = inspect.signature(AppleMusicApi.create_from_netscape_cookies)
api_from_wrapper_sig = inspect.signature(AppleMusicApi.create_from_wrapper)
api_sig = inspect.signature(AppleMusicApi.__init__)
base_downloader_sig = inspect.signature(AppleMusicBaseDownloader.__init__)
music_video_downloader_sig = inspect.signature(AppleMusicMusicVideoDownloader.__init__)
song_downloader_sig = inspect.signature(AppleMusicSongDownloader.__init__)
uploaded_video_downloader_sig = inspect.signature(
    AppleMusicUploadedVideoDownloader.__init__
)


@dataclass
class CliConfig:
    # CLI specific options
    urls: Annotated[
        list[str],
        argument(
            nargs=-1,
            type=str,
            required=True,
        ),
    ]
    read_urls_as_txt: Annotated[
        bool,
        option(
            "--read-urls-as-txt",
            "-r",
            help="Read URLs from text files",
            is_flag=True,
        ),
    ]
    config_path: Annotated[
        str,
        option(
            "--config-path",
            help="Config file path",
            default=str(Path.home() / ".gamdl" / "config.ini"),
            type=click.Path(
                file_okay=True,
                dir_okay=False,
                writable=True,
                resolve_path=True,
            ),
        ),
    ]
    log_level: Annotated[
        str,
        option(
            "--log-level",
            help="Logging level",
            default="INFO",
            type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
        ),
    ]
    log_file: Annotated[
        str,
        option(
            "--log-file",
            help="Log file path",
            default=None,
            type=click.Path(
                file_okay=True,
                dir_okay=False,
                writable=True,
                resolve_path=True,
            ),
        ),
    ]
    no_exceptions: Annotated[
        bool,
        option(
            "--no-exceptions",
            help="Don't print exceptions",
            is_flag=True,
        ),
    ]
    # API specific options
    cookies_path: Annotated[
        str,
        option(
            "--cookies-path",
            "-c",
            help="Cookies file path",
            default=api_from_cookies_sig.parameters["cookies_path"].default,
            type=click.Path(
                file_okay=True,
                dir_okay=False,
                readable=True,
                resolve_path=True,
            ),
        ),
    ]
    wrapper_account_url: Annotated[
        str,
        option(
            "--wrapper-account-url",
            help="Wrapper account URL",
            default=api_from_wrapper_sig.parameters["wrapper_account_url"].default,
        ),
    ]
    language: Annotated[
        str,
        option(
            "--language",
            "-l",
            help="Metadata language",
            default=api_sig.parameters["language"].default,
        ),
    ]
    # Base Downloader specific options
    output_path: Annotated[
        str,
        option(
            "--output-path",
            "-o",
            help="Output directory path",
            default=base_downloader_sig.parameters["output_path"].default,
            type=click.Path(
                file_okay=False,
                dir_okay=True,
                writable=True,
                resolve_path=True,
            ),
        ),
    ]
    temp_path: Annotated[
        str,
        option(
            "--temp-path",
            help="Temporary directory path",
            default=base_downloader_sig.parameters["temp_path"].default,
            type=click.Path(
                file_okay=False,
                dir_okay=True,
                writable=True,
                resolve_path=True,
            ),
        ),
    ]
    wvd_path: Annotated[
        str,
        option(
            "--wvd-path",
            help=".wvd file path",
            default=base_downloader_sig.parameters["wvd_path"].default,
            type=click.Path(
                file_okay=False,
                dir_okay=True,
                writable=True,
                resolve_path=True,
            ),
        ),
    ]
    overwrite: Annotated[
        bool,
        option(
            "--overwrite",
            help="Overwrite existing files",
            is_flag=True,
        ),
    ]
    save_cover: Annotated[
        bool,
        option(
            "--save-cover",
            "-s",
            help="Save cover as separate file",
            is_flag=True,
        ),
    ]
    save_playlist: Annotated[
        bool,
        option(
            "--save-playlist",
            help="Save M3U8 playlist file",
            is_flag=True,
        ),
    ]
    nm3u8dlre_path: Annotated[
        str,
        option(
            "--nm3u8dlre-path",
            help="N_m3u8DL-RE executable path",
            default=base_downloader_sig.parameters["nm3u8dlre_path"].default,
        ),
    ]
    mp4decrypt_path: Annotated[
        str,
        option(
            "--mp4decrypt-path",
            help="mp4decrypt executable path",
            default=base_downloader_sig.parameters["mp4decrypt_path"].default,
        ),
    ]
    ffmpeg_path: Annotated[
        str,
        option(
            "--ffmpeg-path",
            help="FFmpeg executable path",
            default=base_downloader_sig.parameters["ffmpeg_path"].default,
        ),
    ]
    mp4box_path: Annotated[
        str,
        option(
            "--mp4box-path",
            help="MP4Box executable path",
            default=base_downloader_sig.parameters["mp4box_path"].default,
        ),
    ]
    use_wrapper: Annotated[
        bool,
        option(
            "--use-wrapper",
            help="Use wrapper for decrypting songs",
            is_flag=True,
        ),
    ]
    wrapper_decrypt_ip: Annotated[
        str,
        option(
            "--wrapper-decrypt-ip",
            help="IP address and port for wrapper decryption",
            default=base_downloader_sig.parameters["wrapper_decrypt_ip"].default,
        ),
    ]
    download_mode: Annotated[
        DownloadMode,
        option(
            "--download-mode",
            help="Download mode",
            default=base_downloader_sig.parameters["download_mode"].default,
            type=DownloadMode,
        ),
    ]
    remux_mode: Annotated[
        RemuxMode,
        option(
            "--remux-mode",
            help="Remux mode",
            default=base_downloader_sig.parameters["remux_mode"].default,
            type=RemuxMode,
        ),
    ]
    cover_format: Annotated[
        CoverFormat,
        option(
            "--cover-format",
            help="Cover format",
            default=base_downloader_sig.parameters["cover_format"].default,
            type=CoverFormat,
        ),
    ]
    album_folder_template: Annotated[
        str,
        option(
            "--album-folder-template",
            help="Album folder template",
            default=base_downloader_sig.parameters["album_folder_template"].default,
        ),
    ]
    compilation_folder_template: Annotated[
        str,
        option(
            "--compilation-folder-template",
            help="Compilation folder template",
            default=base_downloader_sig.parameters[
                "compilation_folder_template"
            ].default,
        ),
    ]
    no_album_folder_template: Annotated[
        str,
        option(
            "--no-album-folder-template",
            help="No album folder template",
            default=base_downloader_sig.parameters["no_album_folder_template"].default,
        ),
    ]
    single_disc_file_template: Annotated[
        str,
        option(
            "--single-disc-file-template",
            help="Single disc file template",
            default=base_downloader_sig.parameters["single_disc_file_template"].default,
        ),
    ]
    multi_disc_file_template: Annotated[
        str,
        option(
            "--multi-disc-file-template",
            help="Multi disc file template",
            default=base_downloader_sig.parameters["multi_disc_file_template"].default,
        ),
    ]
    no_album_file_template: Annotated[
        str,
        option(
            "--no-album-file-template",
            help="No album file template",
            default=base_downloader_sig.parameters["no_album_file_template"].default,
        ),
    ]
    playlist_file_template: Annotated[
        str,
        option(
            "--playlist-file-template",
            help="Playlist file template",
            default=base_downloader_sig.parameters["playlist_file_template"].default,
        ),
    ]
    date_tag_template: Annotated[
        str,
        option(
            "--date-tag-template",
            help="Date tag template",
            default=base_downloader_sig.parameters["date_tag_template"].default,
        ),
    ]
    exclude_tags: Annotated[
        list[str],
        option(
            "--exclude-tags",
            help="Comma-separated tags to exclude",
            default=base_downloader_sig.parameters["exclude_tags"].default,
            type=Csv(str),
        ),
    ]
    cover_size: Annotated[
        int,
        option(
            "--cover-size",
            help="Cover size in pixels",
            default=base_downloader_sig.parameters["cover_size"].default,
        ),
    ]
    truncate: Annotated[
        int,
        option(
            "--truncate",
            help="Max filename length",
            default=base_downloader_sig.parameters["truncate"].default,
        ),
    ]
    # DownloaderSong specific options
    song_codec: Annotated[
        SongCodec,
        option(
            "--song-codec",
            help="Song codec",
            default=song_downloader_sig.parameters["codec"].default,
            type=SongCodec,
        ),
    ]
    synced_lyrics_format: Annotated[
        SyncedLyricsFormat,
        option(
            "--synced-lyrics-format",
            help="Synced lyrics format",
            default=song_downloader_sig.parameters["synced_lyrics_format"].default,
            type=SyncedLyricsFormat,
        ),
    ]
    no_synced_lyrics: Annotated[
        bool,
        option(
            "--no-synced-lyrics",
            help="Don't download synced lyrics",
            is_flag=True,
        ),
    ]
    synced_lyrics_only: Annotated[
        bool,
        option(
            "--synced-lyrics-only",
            help="Download only synced lyrics",
            is_flag=True,
        ),
    ]
    use_album_date: Annotated[
        bool,
        option(
            "--use-album-date",
            help="Use album release date for songs",
            is_flag=True,
        ),
    ]
    fetch_extra_tags: Annotated[
        bool,
        option(
            "--fetch-extra-tags",
            help="Fetch extra tags from preview (normalization and smooth playback)",
            is_flag=True,
        ),
    ]
    # DownloaderMusicVideo specific options
    music_video_codec_priority: Annotated[
        list[MusicVideoCodec],
        option(
            "--music-video-codec-priority",
            help="Comma-separated codec priority",
            default=music_video_downloader_sig.parameters["codec_priority"].default,
            type=Csv(MusicVideoCodec),
        ),
    ]
    music_video_remux_format: Annotated[
        RemuxFormatMusicVideo,
        option(
            "--music-video-remux-format",
            help="Music video remux format",
            default=music_video_downloader_sig.parameters["remux_format"].default,
            type=RemuxFormatMusicVideo,
        ),
    ]
    music_video_resolution: Annotated[
        MusicVideoResolution,
        option(
            "--music-video-resolution",
            help="Max music video resolution",
            default=music_video_downloader_sig.parameters["resolution"].default,
            type=MusicVideoResolution,
        ),
    ]
    # DownloaderUploadedVideo specific options
    uploaded_video_quality: Annotated[
        UploadedVideoQuality,
        option(
            "--uploaded-video-quality",
            help="Post video quality",
            default=uploaded_video_downloader_sig.parameters["quality"].default,
            type=UploadedVideoQuality,
        ),
    ]
    no_config_file: Annotated[
        bool,
        option(
            "--no-config-file",
            "-n",
            help="Don't use a config file",
            is_flag=True,
        ),
    ]
