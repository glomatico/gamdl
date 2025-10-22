import asyncio
import inspect
import logging
import typing
from functools import wraps
from pathlib import Path

import click

from .. import __version__
from ..api.apple_music_api import AppleMusicApi
from ..downloader.downloader import AppleMusicDownloader
from ..downloader.downloader_base import AppleMusicBaseDownloader
from ..downloader.downloader_music_video import AppleMusicMusicVideoDownloader
from ..downloader.downloader_song import AppleMusicSongDownloader
from ..downloader.downloader_uploaded_video import AppleMusicUploadedVideoDownloader
from ..downloader.enums import (
    CoverFormat,
    DownloadMode,
    RemuxFormatMusicVideo,
    RemuxMode,
)
from ..downloader.exceptions import (
    MediaFormatNotAvailableError,
    MediaNotStreamableError,
)
from ..interface.enums import (
    MusicVideoCodec,
    MusicVideoResolution,
    SongCodec,
    SyncedLyricsFormat,
    UploadedVideoQuality,
)
from .config_file import ConfigFile
from .custom_logger_formatter import CustomLoggerFormatter

logger = logging.getLogger(__name__)

api_sig = inspect.signature(AppleMusicApi.from_netscape_cookies)
base_downloader_sig = inspect.signature(AppleMusicBaseDownloader.__init__)
music_video_downloader_sig = inspect.signature(AppleMusicMusicVideoDownloader.__init__)
song_downloader_sig = inspect.signature(AppleMusicSongDownloader.__init__)
uploaded_video_downloader_sig = inspect.signature(
    AppleMusicUploadedVideoDownloader.__init__
)


class Csv(click.ParamType):
    name = "csv"

    def __init__(
        self,
        subtype: typing.Any,
    ) -> None:
        self.subtype = subtype

    def convert(
        self,
        value: str | typing.Any,
        param: click.Parameter,
        ctx: click.Context,
    ) -> list[typing.Any]:
        if not isinstance(value, str):
            return value

        items = [v.strip() for v in value.split(",") if v.strip()]
        result = []

        for item in items:
            try:
                result.append(self.subtype(item))
            except ValueError as e:
                self.fail(
                    f"'{item}' is not a valid value for {self.subtype.__name__}",
                    param,
                    ctx,
                )
        return result


class PathPrompt(click.ParamType):
    name = "path"

    def __init__(self, is_file: bool = False) -> None:
        self.is_file = is_file

    def convert(
        self,
        value: str | typing.Any,
        param: click.Parameter,
        ctx: click.Context,
    ) -> str:
        if not isinstance(value, str):
            return value

        path_validator = click.Path(
            exists=True,
            file_okay=self.is_file,
            dir_okay=not self.is_file,
        )
        path_type = "file" if self.is_file else "directory"
        while True:
            try:
                result = path_validator.convert(value, None, None)
                break
            except click.BadParameter as e:
                value = click.prompt(
                    (
                        f'{path_type.capitalize()} "{Path(value).absolute()}" does not exist. '
                        f"Create the {path_type} at the specified path, "
                        f"type a new path or drag and drop the {path_type} here. "
                        "Then, press enter to continue"
                    ),
                    default=value,
                    show_default=False,
                )
                value = value.strip('"')
        return result


def load_config_file(
    ctx: click.Context,
    param: click.Parameter,
    no_config_file: bool,
) -> click.Context:
    if no_config_file:
        return ctx

    config_file = ConfigFile(ctx.params["config_path"])
    config_file.add_params_default_to_config(
        ctx.command.params,
    )
    parsed_params = config_file.parse_params_from_config(
        [
            param
            for param in ctx.command.params
            if ctx.get_parameter_source(param.name)
            != click.core.ParameterSource.COMMANDLINE
        ]
    )
    ctx.params.update(parsed_params)

    return ctx


def make_sync(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return wrapper


@click.command()
@click.help_option("-h", "--help")
@click.version_option(__version__, "-v", "--version")
# CLI specific options
@click.argument(
    "urls",
    nargs=-1,
    type=str,
    required=True,
)
@click.option(
    "--read-urls-as-txt",
    "-r",
    is_flag=True,
    help="Interpret URLs as paths to text files containing URLs separated by newlines",
)
@click.option(
    "--config-path",
    type=click.Path(file_okay=True, dir_okay=False, writable=True, resolve_path=True),
    default=str(Path.home() / ".gamdl" / "config.ini"),
    help="Path to config file.",
)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
    default="INFO",
    help="Log level.",
)
@click.option(
    "--log-file",
    type=click.Path(file_okay=True, dir_okay=False, writable=True, resolve_path=True),
    default=None,
    help="Path to log file.",
)
@click.option(
    "--no-exceptions",
    is_flag=True,
    help="Don't print exceptions.",
)
# API specific options
@click.option(
    "--cookies-path",
    "-c",
    type=PathPrompt(is_file=True),
    default=api_sig.parameters["cookies_path"].default,
    help="Path to .txt cookies file.",
)
@click.option(
    "--language",
    "-l",
    type=str,
    default=api_sig.parameters["language"].default,
    help="Metadata language as an ISO-2A language code (don't always work for videos).",
)
# Base Downloader specific options
@click.option(
    "--output-path",
    "-o",
    type=click.Path(file_okay=False, dir_okay=True, writable=True, resolve_path=True),
    default=base_downloader_sig.parameters["output_path"].default,
    help="Path to output directory.",
)
@click.option(
    "--temp-path",
    type=click.Path(file_okay=False, dir_okay=True, writable=True, resolve_path=True),
    default=base_downloader_sig.parameters["temp_path"].default,
    help="Path to temporary directory.",
)
@click.option(
    "--wvd-path",
    type=click.Path(file_okay=False, dir_okay=True, writable=True, resolve_path=True),
    default=base_downloader_sig.parameters["wvd_path"].default,
    help="Path to .wvd file.",
)
@click.option(
    "--overwrite",
    is_flag=True,
    help="Overwrite existing files.",
    default=base_downloader_sig.parameters["overwrite"].default,
)
@click.option(
    "--save-cover",
    "-s",
    is_flag=True,
    help="Save cover as a separate file.",
    default=base_downloader_sig.parameters["save_cover"].default,
)
@click.option(
    "--save-playlist",
    is_flag=True,
    help="Save a M3U8 playlist file when downloading a playlist.",
    default=base_downloader_sig.parameters["save_playlist"].default,
)
@click.option(
    "--nm3u8dlre-path",
    type=str,
    default=base_downloader_sig.parameters["nm3u8dlre_path"].default,
    help="Path to N_m3u8DL-RE binary.",
)
@click.option(
    "--mp4decrypt-path",
    type=str,
    default=base_downloader_sig.parameters["mp4decrypt_path"].default,
    help="Path to mp4decrypt binary.",
)
@click.option(
    "--ffmpeg-path",
    type=str,
    default=base_downloader_sig.parameters["ffmpeg_path"].default,
    help="Path to FFmpeg binary.",
)
@click.option(
    "--mp4box-path",
    type=str,
    default=base_downloader_sig.parameters["mp4box_path"].default,
    help="Path to MP4Box binary.",
)
@click.option(
    "--download-mode",
    type=DownloadMode,
    default=base_downloader_sig.parameters["download_mode"].default,
    help="Download mode.",
)
@click.option(
    "--remux-mode",
    type=RemuxMode,
    default=base_downloader_sig.parameters["remux_mode"].default,
    help="Remux mode.",
)
@click.option(
    "--cover-format",
    type=CoverFormat,
    default=base_downloader_sig.parameters["cover_format"].default,
    help="Cover format.",
)
@click.option(
    "--album-folder-template",
    type=str,
    default=base_downloader_sig.parameters["album_folder_template"].default,
    help="Template folder for tracks that are part of an album.",
)
@click.option(
    "--compilation-folder-template",
    type=str,
    default=base_downloader_sig.parameters["compilation_folder_template"].default,
    help="Template folder for tracks that are part of a compilation album.",
)
@click.option(
    "--single-disc-folder-template",
    type=str,
    default=base_downloader_sig.parameters["single_disc_folder_template"].default,
    help="Template file for the tracks that are part of a single-disc album.",
)
@click.option(
    "--multi-disc-folder-template",
    type=str,
    default=base_downloader_sig.parameters["multi_disc_folder_template"].default,
    help="Template file for the tracks that are part of a multi-disc album.",
)
@click.option(
    "--no-album-folder-template",
    type=str,
    default=base_downloader_sig.parameters["no_album_folder_template"].default,
    help="Template folder for the tracks that are not part of an album.",
)
@click.option(
    "--no-album-file-template",
    type=str,
    default=base_downloader_sig.parameters["no_album_file_template"].default,
    help="Template file for the tracks that are not part of an album.",
)
@click.option(
    "--playlist-file-template",
    type=str,
    default=base_downloader_sig.parameters["playlist_file_template"].default,
    help="Template file for the M3U8 playlist.",
)
@click.option(
    "--date-tag-template",
    type=str,
    default=base_downloader_sig.parameters["date_tag_template"].default,
    help="Date tag template.",
)
@click.option(
    "--exclude-tags",
    type=Csv(str),
    default=base_downloader_sig.parameters["exclude_tags"].default,
    help="Comma-separated tags to exclude.",
)
@click.option(
    "--cover-size",
    type=int,
    default=base_downloader_sig.parameters["cover_size"].default,
    help="Cover size.",
)
@click.option(
    "--truncate",
    type=int,
    default=base_downloader_sig.parameters["truncate"].default,
    help="Maximum length of the file/folder names.",
)
@click.option(
    "--database-path",
    type=click.Path(file_okay=True, dir_okay=False, writable=True, resolve_path=True),
    default=base_downloader_sig.parameters["database_path"].default,
    help="Path to the downloaded media database file.",
)
# DownloaderSong specific options
@click.option(
    "--codec-song",
    type=SongCodec,
    default=song_downloader_sig.parameters["codec"].default,
    help="Song codec.",
)
@click.option(
    "--synced-lyrics-format",
    type=SyncedLyricsFormat,
    default=song_downloader_sig.parameters["synced_lyrics_format"].default,
    help="Synced lyrics format.",
)
@click.option(
    "--no-synced-lyrics",
    is_flag=True,
    help="Don't download the synced lyrics.",
    default=song_downloader_sig.parameters["no_synced_lyrics"].default,
)
@click.option(
    "--synced-lyrics-only",
    is_flag=True,
    help="Download only the synced lyrics.",
    default=song_downloader_sig.parameters["synced_lyrics_only"].default,
)
# DownloaderMusicVideo specific options
@click.option(
    "--music-video-codec-priority",
    type=Csv(MusicVideoCodec),
    default=music_video_downloader_sig.parameters["codec_priority"].default,
    help="Comma-separated music video codec priority.",
)
@click.option(
    "--music-video-remux-format",
    type=RemuxFormatMusicVideo,
    default=music_video_downloader_sig.parameters["remux_format"].default,
    help="Music video remux format.",
)
@click.option(
    "--music-video-resolution",
    type=MusicVideoResolution,
    default=music_video_downloader_sig.parameters["resolution"].default,
    help="Target video resolution for music videos.",
)
# DownloaderPost specific options
@click.option(
    "--quality-post",
    type=UploadedVideoQuality,
    default=uploaded_video_downloader_sig.parameters["quality"].default,
    help="Upload videos quality.",
)
# This option should always be last
@click.option(
    "--no-config-file",
    "-n",
    is_flag=True,
    callback=load_config_file,
    help="Do not use a config file.",
)
@make_sync
async def main(
    urls: list[str],
    read_urls_as_txt: bool,
    config_path: str,
    log_level: str,
    log_file: str,
    no_exceptions: bool,
    cookies_path: str,
    language: str,
    output_path: str,
    temp_path: str,
    wvd_path: str,
    overwrite: bool,
    save_cover: bool,
    save_playlist: bool,
    nm3u8dlre_path: str,
    mp4decrypt_path: str,
    ffmpeg_path: str,
    mp4box_path: str,
    download_mode: DownloadMode,
    remux_mode: RemuxMode,
    cover_format: CoverFormat,
    album_folder_template: str,
    compilation_folder_template: str,
    single_disc_folder_template: str,
    multi_disc_folder_template: str,
    no_album_folder_template: str,
    no_album_file_template: str,
    playlist_file_template: str,
    date_tag_template: str,
    exclude_tags: list[str],
    cover_size: int,
    truncate: int,
    database_path: str,
    codec_song: SongCodec,
    synced_lyrics_format: SyncedLyricsFormat,
    no_synced_lyrics: bool,
    synced_lyrics_only: bool,
    music_video_codec_priority: list[MusicVideoCodec],
    music_video_remux_format: RemuxFormatMusicVideo,
    music_video_resolution: MusicVideoResolution,
    quality_post: UploadedVideoQuality,
    *args,
    **kwargs,
):
    root_logger = logging.getLogger(__name__.split(".")[0])
    root_logger.setLevel(log_level)
    root_logger.propagate = False

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(CustomLoggerFormatter())
    root_logger.addHandler(stream_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(CustomLoggerFormatter(use_colors=False))
        root_logger.addHandler(file_handler)

    logger.info(f"Starting Gamdl {__version__}")

    api = AppleMusicApi.from_netscape_cookies(
        cookies_path=cookies_path,
        language=language,
    )
    await api.setup()

    if not api.account_info["meta"]["subscription"]["active"]:
        logger.critical(
            "No active Apple Music subscription found, you won't be able to download"
            " anything"
        )
        return
    if api.account_info["data"][0]["attributes"].get("restrictions"):
        logger.warning(
            "Your account has content restrictions enabled, some content may not be"
            " downloadable"
        )

    base_downloader = AppleMusicBaseDownloader(
        apple_music_api=api,
        output_path=output_path,
        temp_path=temp_path,
        wvd_path=wvd_path,
        overwrite=overwrite,
        save_cover=save_cover,
        save_playlist=save_playlist,
        nm3u8dlre_path=nm3u8dlre_path,
        mp4decrypt_path=mp4decrypt_path,
        ffmpeg_path=ffmpeg_path,
        mp4box_path=mp4box_path,
        download_mode=download_mode,
        remux_mode=remux_mode,
        cover_format=cover_format,
        album_folder_template=album_folder_template,
        compilation_folder_template=compilation_folder_template,
        single_disc_folder_template=single_disc_folder_template,
        multi_disc_folder_template=multi_disc_folder_template,
        no_album_folder_template=no_album_folder_template,
        no_album_file_template=no_album_file_template,
        playlist_file_template=playlist_file_template,
        date_tag_template=date_tag_template,
        exclude_tags=exclude_tags,
        cover_size=cover_size,
        truncate=truncate,
        database_path=database_path,
    )
    base_downloader.setup()

    song_downloader = AppleMusicSongDownloader(
        base_downloader,
        codec=codec_song,
        synced_lyrics_format=synced_lyrics_format,
        no_synced_lyrics=no_synced_lyrics,
        synced_lyrics_only=synced_lyrics_only,
    )
    song_downloader.setup()

    music_video_downloader = AppleMusicMusicVideoDownloader(
        base_downloader,
        codec_priority=music_video_codec_priority,
        remux_format=music_video_remux_format,
        resolution=music_video_resolution,
    )
    music_video_downloader.setup()

    uploaded_video_downloader = AppleMusicUploadedVideoDownloader(
        base_downloader,
        quality=quality_post,
    )
    uploaded_video_downloader.setup()

    downloader = AppleMusicDownloader(
        base_downloader,
        song_downloader,
        music_video_downloader,
        uploaded_video_downloader,
    )

    if read_urls_as_txt:
        urls_from_file = []
        for url in urls:
            if Path(url).is_file() and Path(url).exists():
                urls_from_file.extend(
                    [
                        line.strip()
                        for line in Path(url).read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                )
        urls = urls_from_file

    error_count = 0
    for url_index, url in enumerate(urls, 1):
        url_progress = click.style(f"[URL {url_index}/{len(urls)}]", dim=True)
        logger.info(url_progress + f' Processing "{url}"')
        download_queue = None
        try:
            url_info = downloader.get_url_info(url)
            if not url_info:
                logger.warning(
                    url_progress + f' Could not parse "{url}", skipping.',
                )
                continue

            download_queue = await downloader.get_download_queue(url_info)
            if not download_queue:
                logger.warning(
                    url_progress
                    + f' No downloadable media found for "{url}", skipping.',
                )
                continue
        except KeyboardInterrupt:
            exit(1)
        except Exception as e:
            error_count += 1
            logger.error(
                url_progress + f' Error processing "{url}"',
                exc_info=not no_exceptions,
            )
        if not download_queue:
            continue
        for download_index, download_item in enumerate(
            download_queue,
            1,
        ):
            download_queue_progress = click.style(
                f"[Track {download_index}/{len(download_queue)}]",
                dim=True,
            )
            media_title = download_item.media_metadata["attributes"]["name"]
            logger.info(download_queue_progress + f' Downloading "{media_title}"')
            try:
                await downloader.download(download_item)
            except (
                FileExistsError,
                MediaNotStreamableError,
                MediaFormatNotAvailableError,
            ) as e:
                logger.warning(
                    download_queue_progress + f' Skipping "{media_title}": {e}'
                )
                continue
            except KeyboardInterrupt:
                exit(1)
            except Exception as e:
                error_count += 1
                logger.error(
                    download_queue_progress + f' Error downloading "{media_title}"',
                    exc_info=not no_exceptions,
                )

    logger.info(f"Finished with {error_count} error(s)")
