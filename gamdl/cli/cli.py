import asyncio
import inspect
import logging
from functools import wraps
from pathlib import Path

import click

from .. import __version__
from ..api import AppleMusicApi, ItunesApi
from ..downloader import (
    AppleMusicBaseDownloader,
    AppleMusicDownloader,
    AppleMusicMusicVideoDownloader,
    AppleMusicSongDownloader,
    AppleMusicUploadedVideoDownloader,
    CoverFormat,
    DownloadItem,
    DownloadMode,
    GamdlFormatNotAvailableError,
    GamdlNotStreamableError,
    GamdlSyncedLyricsOnlyError,
    RemuxFormatMusicVideo,
    RemuxMode,
)
from ..interface import (
    AppleMusicInterface,
    AppleMusicMusicVideoInterface,
    AppleMusicSongInterface,
    AppleMusicUploadedVideoInterface,
    MusicVideoCodec,
    MusicVideoResolution,
    SongCodec,
    SyncedLyricsFormat,
    UploadedVideoQuality,
)
from .config_file import ConfigFile
from .constants import X_NOT_IN_PATH
from .utils import Csv, CustomLoggerFormatter, prompt_path

logger = logging.getLogger(__name__)

api_sig = inspect.signature(AppleMusicApi.from_netscape_cookies)
base_downloader_sig = inspect.signature(AppleMusicBaseDownloader.__init__)
music_video_downloader_sig = inspect.signature(AppleMusicMusicVideoDownloader.__init__)
song_downloader_sig = inspect.signature(AppleMusicSongDownloader.__init__)
uploaded_video_downloader_sig = inspect.signature(
    AppleMusicUploadedVideoDownloader.__init__
)


def load_config_file(
    ctx: click.Context,
    param: click.Parameter,
    no_config_file: bool,
) -> click.Context:
    if no_config_file:
        return ctx

    config_file = ConfigFile(ctx.params["config_path"])
    config_file.cleanup_unknown_params(ctx.command.params)
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
    help="Read URLs from text files",
)
@click.option(
    "--config-path",
    type=click.Path(file_okay=True, dir_okay=False, writable=True, resolve_path=True),
    default=str(Path.home() / ".gamdl" / "config.ini"),
    help="Config file path",
)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
    default="INFO",
    help="Logging level",
)
@click.option(
    "--log-file",
    type=click.Path(file_okay=True, dir_okay=False, writable=True, resolve_path=True),
    default=None,
    help="Log file path",
)
@click.option(
    "--no-exceptions",
    is_flag=True,
    help="Don't print exceptions",
)
# API specific options
@click.option(
    "--cookies-path",
    "-c",
    type=click.Path(file_okay=True, dir_okay=False, readable=True, resolve_path=True),
    default=api_sig.parameters["cookies_path"].default,
    help="Cookies file path",
)
@click.option(
    "--language",
    "-l",
    type=str,
    default=api_sig.parameters["language"].default,
    help="Metadata language",
)
# Base Downloader specific options
@click.option(
    "--output-path",
    "-o",
    type=click.Path(file_okay=False, dir_okay=True, writable=True, resolve_path=True),
    default=base_downloader_sig.parameters["output_path"].default,
    help="Output directory path",
)
@click.option(
    "--temp-path",
    type=click.Path(file_okay=False, dir_okay=True, writable=True, resolve_path=True),
    default=base_downloader_sig.parameters["temp_path"].default,
    help="Temporary directory path",
)
@click.option(
    "--wvd-path",
    type=click.Path(file_okay=False, dir_okay=True, writable=True, resolve_path=True),
    default=base_downloader_sig.parameters["wvd_path"].default,
    help=".wvd file path",
)
@click.option(
    "--overwrite",
    is_flag=True,
    help="Overwrite existing files",
    default=base_downloader_sig.parameters["overwrite"].default,
)
@click.option(
    "--save-cover",
    "-s",
    is_flag=True,
    help="Save cover as separate file",
    default=base_downloader_sig.parameters["save_cover"].default,
)
@click.option(
    "--save-playlist",
    is_flag=True,
    help="Save M3U8 playlist file",
    default=base_downloader_sig.parameters["save_playlist"].default,
)
@click.option(
    "--nm3u8dlre-path",
    type=str,
    default=base_downloader_sig.parameters["nm3u8dlre_path"].default,
    help="N_m3u8DL-RE executable path",
)
@click.option(
    "--mp4decrypt-path",
    type=str,
    default=base_downloader_sig.parameters["mp4decrypt_path"].default,
    help="mp4decrypt executable path",
)
@click.option(
    "--ffmpeg-path",
    type=str,
    default=base_downloader_sig.parameters["ffmpeg_path"].default,
    help="FFmpeg executable path",
)
@click.option(
    "--mp4box-path",
    type=str,
    default=base_downloader_sig.parameters["mp4box_path"].default,
    help="MP4Box executable path",
)
@click.option(
    "--download-mode",
    type=DownloadMode,
    default=base_downloader_sig.parameters["download_mode"].default,
    help="Download mode",
)
@click.option(
    "--remux-mode",
    type=RemuxMode,
    default=base_downloader_sig.parameters["remux_mode"].default,
    help="Remux mode",
)
@click.option(
    "--cover-format",
    type=CoverFormat,
    default=base_downloader_sig.parameters["cover_format"].default,
    help="Cover format",
)
@click.option(
    "--album-folder-template",
    type=str,
    default=base_downloader_sig.parameters["album_folder_template"].default,
    help="Album folder template",
)
@click.option(
    "--compilation-folder-template",
    type=str,
    default=base_downloader_sig.parameters["compilation_folder_template"].default,
    help="Compilation folder template",
)
@click.option(
    "--no-album-folder-template",
    type=str,
    default=base_downloader_sig.parameters["no_album_folder_template"].default,
    help="No album folder template",
)
@click.option(
    "--single-disc-file-template",
    type=str,
    default=base_downloader_sig.parameters["single_disc_file_template"].default,
    help="Single disc file template",
)
@click.option(
    "--multi-disc-file-template",
    type=str,
    default=base_downloader_sig.parameters["multi_disc_file_template"].default,
    help="Multi disc file template",
)
@click.option(
    "--no-album-file-template",
    type=str,
    default=base_downloader_sig.parameters["no_album_file_template"].default,
    help="No album file template",
)
@click.option(
    "--playlist-file-template",
    type=str,
    default=base_downloader_sig.parameters["playlist_file_template"].default,
    help="Playlist file template",
)
@click.option(
    "--date-tag-template",
    type=str,
    default=base_downloader_sig.parameters["date_tag_template"].default,
    help="Date tag template",
)
@click.option(
    "--exclude-tags",
    type=Csv(str),
    default=base_downloader_sig.parameters["exclude_tags"].default,
    help="Comma-separated tags to exclude",
)
@click.option(
    "--cover-size",
    type=int,
    default=base_downloader_sig.parameters["cover_size"].default,
    help="Cover size in pixels",
)
@click.option(
    "--truncate",
    type=int,
    default=base_downloader_sig.parameters["truncate"].default,
    help="Max filename length",
)
# DownloaderSong specific options
@click.option(
    "--song-codec",
    type=SongCodec,
    default=song_downloader_sig.parameters["codec"].default,
    help="Song codec",
)
@click.option(
    "--synced-lyrics-format",
    type=SyncedLyricsFormat,
    default=song_downloader_sig.parameters["synced_lyrics_format"].default,
    help="Synced lyrics format",
)
@click.option(
    "--no-synced-lyrics",
    is_flag=True,
    help="Don't download synced lyrics",
    default=song_downloader_sig.parameters["no_synced_lyrics"].default,
)
@click.option(
    "--synced-lyrics-only",
    is_flag=True,
    help="Download only synced lyrics",
    default=song_downloader_sig.parameters["synced_lyrics_only"].default,
)
# DownloaderMusicVideo specific options
@click.option(
    "--music-video-codec-priority",
    type=Csv(MusicVideoCodec),
    default=music_video_downloader_sig.parameters["codec_priority"].default,
    help="Comma-separated codec priority",
)
@click.option(
    "--music-video-remux-format",
    type=RemuxFormatMusicVideo,
    default=music_video_downloader_sig.parameters["remux_format"].default,
    help="Music video remux format",
)
@click.option(
    "--music-video-resolution",
    type=MusicVideoResolution,
    default=music_video_downloader_sig.parameters["resolution"].default,
    help="Max music video resolution",
)
# DownloaderUploadedVideo specific options
@click.option(
    "--uploaded-video-quality",
    type=UploadedVideoQuality,
    default=uploaded_video_downloader_sig.parameters["quality"].default,
    help="Post video quality",
)
# This option should always be last
@click.option(
    "--no-config-file",
    "-n",
    is_flag=True,
    callback=load_config_file,
    help="Don't use a config file",
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
    no_album_folder_template: str,
    single_disc_file_template: str,
    multi_disc_file_template: str,
    no_album_file_template: str,
    playlist_file_template: str,
    date_tag_template: str,
    exclude_tags: list[str],
    cover_size: int,
    truncate: int,
    song_codec: SongCodec,
    synced_lyrics_format: SyncedLyricsFormat,
    no_synced_lyrics: bool,
    synced_lyrics_only: bool,
    music_video_codec_priority: list[MusicVideoCodec],
    music_video_remux_format: RemuxFormatMusicVideo,
    music_video_resolution: MusicVideoResolution,
    uploaded_video_quality: UploadedVideoQuality,
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

    cookies_path = prompt_path(cookies_path)

    logger.info(f"Starting Gamdl {__version__}")

    apple_music_api = AppleMusicApi.from_netscape_cookies(
        cookies_path=cookies_path,
        language=language,
    )
    await apple_music_api.setup()

    itunes_api = ItunesApi(
        apple_music_api.storefront,
        apple_music_api.language,
    )
    itunes_api.setup()

    if not apple_music_api.account_info["meta"]["subscription"]["active"]:
        logger.critical(
            "No active Apple Music subscription found, you won't be able to download"
            " anything"
        )
        return
    if apple_music_api.account_info["data"][0]["attributes"].get("restrictions"):
        logger.warning(
            "Your account has content restrictions enabled, some content may not be"
            " downloadable"
        )

    interface = AppleMusicInterface(
        apple_music_api,
        itunes_api,
    )
    song_interface = AppleMusicSongInterface(interface)
    music_video_interface = AppleMusicMusicVideoInterface(interface)
    uploaded_video_interface = AppleMusicUploadedVideoInterface(interface)

    base_downloader = AppleMusicBaseDownloader(
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
        no_album_folder_template=no_album_folder_template,
        single_disc_file_template=single_disc_file_template,
        multi_disc_file_template=multi_disc_file_template,
        no_album_file_template=no_album_file_template,
        playlist_file_template=playlist_file_template,
        date_tag_template=date_tag_template,
        exclude_tags=exclude_tags,
        cover_size=cover_size,
        truncate=truncate,
    )
    base_downloader.setup()
    song_downloader = AppleMusicSongDownloader(
        base_downloader=base_downloader,
        interface=song_interface,
        codec=song_codec,
        synced_lyrics_format=synced_lyrics_format,
        no_synced_lyrics=no_synced_lyrics,
        synced_lyrics_only=synced_lyrics_only,
    )
    music_video_downloader = AppleMusicMusicVideoDownloader(
        base_downloader=base_downloader,
        interface=music_video_interface,
        codec_priority=music_video_codec_priority,
        remux_format=music_video_remux_format,
        resolution=music_video_resolution,
    )
    uploaded_video_downloader = AppleMusicUploadedVideoDownloader(
        base_downloader=base_downloader,
        interface=uploaded_video_interface,
        quality=uploaded_video_quality,
    )
    downloader = AppleMusicDownloader(
        interface=interface,
        base_downloader=base_downloader,
        song_downloader=song_downloader,
        music_video_downloader=music_video_downloader,
        uploaded_video_downloader=uploaded_video_downloader,
    )

    if not synced_lyrics_only:
        if not base_downloader.full_ffmpeg_path and (
            remux_mode == RemuxMode.FFMPEG or download_mode == DownloadMode.NM3U8DLRE
        ):
            logger.critical(X_NOT_IN_PATH.format("ffmpeg", ffmpeg_path))
            return

        if not base_downloader.full_mp4box_path and remux_mode == RemuxMode.MP4BOX:
            logger.critical(X_NOT_IN_PATH.format("MP4Box", mp4box_path))
            return

        if (
            not base_downloader.full_mp4decrypt_path
            and song_codec
            not in (
                SongCodec.AAC_LEGACY,
                SongCodec.AAC_HE_LEGACY,
            )
            or (
                remux_mode == RemuxMode.MP4BOX
                and not base_downloader.full_mp4decrypt_path
            )
        ):
            logger.critical(X_NOT_IN_PATH.format("mp4decrypt", mp4decrypt_path))
            return

        if (
            download_mode == DownloadMode.NM3U8DLRE
            and not base_downloader.full_nm3u8dlre_path
        ):
            logger.critical(X_NOT_IN_PATH.format("N_m3u8DL-RE", nm3u8dlre_path))
            return

        if not base_downloader.full_mp4decrypt_path:
            logger.warning(
                X_NOT_IN_PATH.format("mp4decrypt", mp4decrypt_path)
                + ", music videos will not be downloaded"
            )
            downloader.skip_music_videos = True

        if not song_codec.is_legacy():
            logger.warning(
                "You have chosen an experimental song codec. "
                "They're not guaranteed to work due to API limitations."
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
        url_value = url.strip()
        url_progress = click.style(f"[URL {url_index}/{len(urls)}]", dim=True)
        logger.info(url_progress + f' Processing "{url_value}"')
        download_queue = None
        try:
            if url_value.lower() == "library":
                download_queue = await downloader.get_download_queue_library()
                if not download_queue:
                    logger.warning(
                        url_progress
                        + ' No downloadable media found for "library", skipping.',
                    )
                    continue
            else:
                url_info = downloader.get_url_info(url_value)
                if not url_info:
                    logger.warning(
                        url_progress + f' Could not parse "{url_value}", skipping.',
                    )
                    continue

                download_queue = await downloader.get_download_queue(url_info)
                if not download_queue:
                    logger.warning(
                        url_progress
                        + f' No downloadable media found for "{url_value}", skipping.',
                    )
                    continue
        except KeyboardInterrupt:
            exit(1)
        except Exception as e:
            error_count += 1
            logger.error(
                url_progress + f' Error processing "{url_value}"',
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
            media_title = (
                download_item.media_metadata["attributes"]["name"]
                if isinstance(
                    download_item,
                    DownloadItem,
                )
                else "Unknown Title"
            )
            logger.info(download_queue_progress + f' Downloading "{media_title}"')

            try:
                await downloader.download(download_item)
            except (
                FileExistsError,
                GamdlNotStreamableError,
                GamdlFormatNotAvailableError,
                GamdlSyncedLyricsOnlyError,
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
