import asyncio
import logging
from functools import wraps
from pathlib import Path

import click
import colorama
from dataclass_click import dataclass_click
from httpx import ConnectError

from .. import __version__
from ..api import AppleMusicApi, ItunesApi
from ..downloader import (
    AppleMusicBaseDownloader,
    AppleMusicDownloader,
    AppleMusicMusicVideoDownloader,
    AppleMusicSongDownloader,
    AppleMusicUploadedVideoDownloader,
    DownloadItem,
    DownloadMode,
    GamdlError,
    RemuxMode,
)
from ..interface import (
    AppleMusicInterface,
    AppleMusicMusicVideoInterface,
    AppleMusicSongInterface,
    AppleMusicUploadedVideoInterface,
    SongCodec,
)
from .cli_config import CliConfig
from .config_file import ConfigFile
from .constants import X_NOT_IN_PATH
from .utils import CustomLoggerFormatter, prompt_path

logger = logging.getLogger(__name__)


def make_sync(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return wrapper


@click.command()
@click.help_option("-h", "--help")
@click.version_option(__version__, "-v", "--version")
@dataclass_click(CliConfig)
@ConfigFile.loader
@make_sync
async def main(config: CliConfig):
    colorama.just_fix_windows_console()

    root_logger = logging.getLogger(__name__.split(".")[0])
    root_logger.setLevel(config.log_level)
    root_logger.propagate = False

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(CustomLoggerFormatter())
    root_logger.addHandler(stream_handler)

    if config.log_file:
        file_handler = logging.FileHandler(config.log_file, encoding="utf-8")
        file_handler.setFormatter(CustomLoggerFormatter(use_colors=False))
        root_logger.addHandler(file_handler)

    logger.info(f"Starting Gamdl {__version__}")

    if config.use_wrapper:
        try:
            apple_music_api = await AppleMusicApi.create_from_wrapper(
                wrapper_account_url=config.wrapper_account_url,
                language=config.language,
            )
        except ConnectError:
            logger.critical(
                "Could not connect to the wrapper account API. "
                "Make sure the wrapper is running and the URL is correct."
            )
            return
    else:
        cookies_path = prompt_path(config.cookies_path)
        apple_music_api = await AppleMusicApi.create_from_netscape_cookies(
            cookies_path=cookies_path,
            language=config.language,
        )

    itunes_api = ItunesApi(
        apple_music_api.storefront,
        apple_music_api.language,
    )

    if not apple_music_api.active_subscription:
        logger.critical(
            "No active Apple Music subscription found, you won't be able to download"
            " anything"
        )
        return
    if apple_music_api.account_restrictions:
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
        output_path=config.output_path,
        temp_path=config.temp_path,
        wvd_path=config.wvd_path,
        overwrite=config.overwrite,
        save_cover=config.save_cover,
        save_playlist=config.save_playlist,
        nm3u8dlre_path=config.nm3u8dlre_path,
        mp4decrypt_path=config.mp4decrypt_path,
        ffmpeg_path=config.ffmpeg_path,
        mp4box_path=config.mp4box_path,
        use_wrapper=config.use_wrapper,
        wrapper_decrypt_ip=config.wrapper_decrypt_ip,
        download_mode=config.download_mode,
        remux_mode=config.remux_mode,
        cover_format=config.cover_format,
        album_folder_template=config.album_folder_template,
        compilation_folder_template=config.compilation_folder_template,
        no_album_folder_template=config.no_album_folder_template,
        single_disc_file_template=config.single_disc_file_template,
        multi_disc_file_template=config.multi_disc_file_template,
        no_album_file_template=config.no_album_file_template,
        playlist_file_template=config.playlist_file_template,
        date_tag_template=config.date_tag_template,
        exclude_tags=config.exclude_tags,
        cover_size=config.cover_size,
        truncate=config.truncate,
    )
    song_downloader = AppleMusicSongDownloader(
        base_downloader=base_downloader,
        interface=song_interface,
        codec=config.song_codec,
        synced_lyrics_format=config.synced_lyrics_format,
        no_synced_lyrics=config.no_synced_lyrics,
        synced_lyrics_only=config.synced_lyrics_only,
        use_album_date=config.use_album_date,
        fetch_extra_tags=config.fetch_extra_tags,
    )
    music_video_downloader = AppleMusicMusicVideoDownloader(
        base_downloader=base_downloader,
        interface=music_video_interface,
        codec_priority=config.music_video_codec_priority,
        remux_format=config.music_video_remux_format,
        resolution=config.music_video_resolution,
    )
    uploaded_video_downloader = AppleMusicUploadedVideoDownloader(
        base_downloader=base_downloader,
        interface=uploaded_video_interface,
        quality=config.uploaded_video_quality,
    )
    downloader = AppleMusicDownloader(
        interface=interface,
        base_downloader=base_downloader,
        song_downloader=song_downloader,
        music_video_downloader=music_video_downloader,
        uploaded_video_downloader=uploaded_video_downloader,
    )

    if not config.synced_lyrics_only:
        if not config.use_wrapper:
            if not base_downloader.full_ffmpeg_path and (
                config.remux_mode == RemuxMode.FFMPEG
                or config.download_mode == DownloadMode.NM3U8DLRE
            ):
                logger.critical(X_NOT_IN_PATH.format("ffmpeg", config.ffmpeg_path))
                return

            if (
                not base_downloader.full_mp4box_path
                and config.remux_mode == RemuxMode.MP4BOX
            ):
                logger.critical(X_NOT_IN_PATH.format("MP4Box", config.mp4box_path))
                return

            if not base_downloader.full_mp4decrypt_path and (
                config.song_codec not in (SongCodec.AAC_LEGACY, SongCodec.AAC_HE_LEGACY)
                or config.remux_mode == RemuxMode.MP4BOX
            ):
                logger.critical(
                    X_NOT_IN_PATH.format("mp4decrypt", config.mp4decrypt_path)
                )
                return

        if (
            config.download_mode == DownloadMode.NM3U8DLRE
            and not base_downloader.full_nm3u8dlre_path
        ):
            logger.critical(X_NOT_IN_PATH.format("N_m3u8DL-RE", config.nm3u8dlre_path))
            return

        if not config.song_codec.is_legacy() and not config.use_wrapper:
            logger.warning(
                "You have chosen an experimental song codec"
                " without enabling wrapper."
                "They're not guaranteed to work due to API limitations."
            )

    if config.read_urls_as_txt:
        urls_from_file = []
        for url in config.urls:
            if Path(url).is_file() and Path(url).exists():
                urls_from_file.extend(
                    [
                        line.strip()
                        for line in Path(url).read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                )
        urls = urls_from_file
    else:
        urls = config.urls

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
                exc_info=not config.no_exceptions,
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
            except GamdlError as e:
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
                    exc_info=not config.no_exceptions,
                )

    logger.info(f"Finished with {error_count} error(s)")
