import asyncio
import logging
from functools import wraps
from pathlib import Path

import click
import colorama
import structlog
from dataclass_click import dataclass_click
from httpx import ConnectError

from .. import __version__
from ..api import AppleMusicApi
from ..downloader import (
    AppleMusicBaseDownloader,
    AppleMusicDownloader,
    AppleMusicMusicVideoDownloader,
    AppleMusicSongDownloader,
    AppleMusicUploadedVideoDownloader,
    DownloadItem,
    GamdlDownloaderDependencyNotFoundError,
    GamdlDownloaderFlatFilterExcludedError,
    GamdlDownloaderMediaFileExistsError,
    GamdlDownloaderSyncedLyricsOnlyError,
)
from ..interface import (
    AppleMusicBaseInterface,
    AppleMusicInterface,
    AppleMusicMusicVideoInterface,
    AppleMusicSongInterface,
    AppleMusicUploadedVideoInterface,
    GamdlInterfaceArtistMediaTypeError,
    GamdlInterfaceDecryptionNotAvailableError,
    GamdlInterfaceFormatNotAvailableError,
    GamdlInterfaceMediaNotStreamableError,
    GamdlInterfaceUrlParseError,
)
from .cli_config import CliConfig
from .config_file import ConfigFile
from .database import Database
from .interactive_prompts import InteractivePrompts
from .utils import custom_structlog_formatter, prompt_path

logger = structlog.get_logger(__name__)


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
    stream_handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(stream_handler)

    if config.log_file:
        file_handler = logging.FileHandler(config.log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        root_logger.addHandler(file_handler)

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.ExceptionPrettyPrinter(),
            custom_structlog_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )

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

    if (
        any(not codec.is_legacy() for codec in config.song_codec_piority)
        and not config.use_wrapper
    ):
        logger.warning(
            "You have chosen an experimental song codec "
            "without enabling wrapper. "
            "They're not guaranteed to work due to API limitations."
        )

    if config.database_path:
        database = Database(config.database_path)
        flat_filter = database.flat_filter
    else:
        database = None
        flat_filter = None

    interactive_prompts = InteractivePrompts(
        artist_auto_select=config.artist_auto_select,
    )

    base_interface = await AppleMusicBaseInterface.create(
        apple_music_api=apple_music_api,
        cover_format=config.cover_format,
        cover_size=config.cover_size,
        use_wrapper=config.use_wrapper,
        wrapper_m3u8_ip=config.wrapper_m3u8_ip,
        wvd_path=config.wvd_path,
    )

    song_interface = AppleMusicSongInterface(
        base=base_interface,
        synced_lyrics_format=config.synced_lyrics_format,
        codec_priority=config.song_codec_piority,
        use_album_date=config.use_album_date,
        skip_decryption_key_non_legacy=config.use_wrapper,
        skip_stream_info=config.synced_lyrics_only,
        ask_codec_function=interactive_prompts.ask_song_codec,
    )
    music_video_interface = AppleMusicMusicVideoInterface(
        base=base_interface,
        resolution=config.music_video_resolution,
        codec_priority=config.music_video_codec_priority,
        ask_video_codec_function=interactive_prompts.ask_music_video_video_codec_function,
        ask_audio_codec_function=interactive_prompts.ask_music_video_audio_codec_function,
    )
    uploaded_video_interface = AppleMusicUploadedVideoInterface(
        base=base_interface,
        quality=config.uploaded_video_quality,
        ask_quality_function=interactive_prompts.ask_uploaded_video_quality_function,
    )

    interface = AppleMusicInterface(
        song=song_interface,
        music_video=music_video_interface,
        uploaded_video=uploaded_video_interface,
        artist_select_media_type_function=interactive_prompts.ask_artist_media_type,
        artist_select_items_function=interactive_prompts.ask_artist_select_items,
        flat_filter_function=flat_filter,
    )

    base_downloader = AppleMusicBaseDownloader(
        interface=interface,
        output_path=config.output_path,
        temp_path=config.temp_path,
        nm3u8dlre_path=config.nm3u8dlre_path,
        mp4decrypt_path=config.mp4decrypt_path,
        ffmpeg_path=config.ffmpeg_path,
        mp4box_path=config.mp4box_path,
        wrapper_decrypt_ip=config.wrapper_decrypt_ip,
        download_mode=config.download_mode,
        album_folder_template=config.album_folder_template,
        compilation_folder_template=config.compilation_folder_template,
        no_album_folder_template=config.no_album_folder_template,
        playlist_folder_template=config.playlist_folder_template,
        single_disc_file_template=config.single_disc_file_template,
        multi_disc_file_template=config.multi_disc_file_template,
        no_album_file_template=config.no_album_file_template,
        playlist_file_template=config.playlist_file_template,
        date_tag_template=config.date_tag_template,
        exclude_tags=config.exclude_tags,
        truncate=config.truncate,
    )

    song_downloader = AppleMusicSongDownloader(
        base=base_downloader,
    )
    music_video_downloader = AppleMusicMusicVideoDownloader(
        base=base_downloader,
        remux_mode=config.music_video_remux_mode,
        remux_format=config.music_video_remux_format,
    )
    uploaded_video_downloader = AppleMusicUploadedVideoDownloader(
        base=base_downloader,
    )

    downloader = AppleMusicDownloader(
        song=song_downloader,
        music_video=music_video_downloader,
        uploaded_video=uploaded_video_downloader,
        overwrite=config.overwrite,
        save_cover=config.save_cover,
        save_playlist=config.save_playlist,
        no_synced_lyrics=config.no_synced_lyrics,
        synced_lyrics_only=config.synced_lyrics_only,
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
        url_log = logger.bind(action=f"URL {url_index:>3}/{len(urls):<3}")

        url_log.info(f'Processing "{url}"')

        try:
            download_queue: list[DownloadItem] = []
            async for media in downloader.get_download_item_from_url(url):
                download_queue.append(media)
        except GamdlInterfaceUrlParseError as e:
            url_log.exception(f"{e}")
            continue
        except Exception as e:
            url_log.exception(f'Error processing "{url}": {e}')
            error_count += 1
            continue

        for download_index, download_item in enumerate(
            download_queue,
            1,
        ):
            track_log = logger.bind(
                action=f"Track {download_index:>3}/{len(download_queue):<3}"
            )

            media_title = (
                download_item.media.media_metadata["attributes"]["name"]
                if download_item.media.media_metadata
                and download_item.media.media_metadata.get("attributes", {}).get("name")
                else "Unknown Title"
            )

            track_log.info(f'Downloading "{media_title}"')

            try:
                await downloader.download(download_item)
            except (
                GamdlInterfaceMediaNotStreamableError,
                GamdlInterfaceFormatNotAvailableError,
                GamdlInterfaceDecryptionNotAvailableError,
                GamdlInterfaceArtistMediaTypeError,
                GamdlDownloaderSyncedLyricsOnlyError,
                GamdlDownloaderMediaFileExistsError,
                GamdlDownloaderDependencyNotFoundError,
                GamdlDownloaderFlatFilterExcludedError,
            ) as e:
                track_log.warning(f'Skipping "{media_title}": {e}')
                continue
            except Exception as e:
                error_count += 1
                track_log.exception(f'Error downloading "{media_title}"')

            if (
                database
                and download_item.media.media_metadata
                and download_item.final_path
            ):
                database.add(
                    download_item.media.media_metadata["id"],
                    download_item.final_path,
                )

    logger.info(f"Finished with {error_count} error(s)")
