import asyncio
import csv
import inspect
import logging
import os
import re
from functools import wraps
from pathlib import Path

import click
import colorama

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
    CoverFormat,
)
from .config_file import ConfigFile
from .constants import X_NOT_IN_PATH, CSV_BATCH_SIZE, CSV_BATCH_DELAY_SECONDS, CSV_RATE_LIMIT_RETRY_SECONDS, CSV_MAX_RETRIES
from .utils import Csv, CustomLoggerFormatter, prompt_path

logger = logging.getLogger(__name__)

api_from_cookies_sig = inspect.signature(AppleMusicApi.create_from_netscape_cookies)
api_from_wrapper_sig = inspect.signature(AppleMusicApi.create_from_wrapper)
api_sig = inspect.signature(AppleMusicApi.__init__)
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
    required=False,
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
    default=api_from_cookies_sig.parameters["cookies_path"].default,
    help="Cookies file path",
)
@click.option(
    "--wrapper-account-url",
    type=str,
    default=api_from_wrapper_sig.parameters["wrapper_account_url"].default,
    help="Wrapper account URL",
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
    "--amdecrypt-path",
    type=str,
    default=base_downloader_sig.parameters["amdecrypt_path"].default,
    help="amdecrypt executable path",
)
@click.option(
    "--use-wrapper",
    is_flag=True,
    help="Use wrapper and amdecrypt for decrypting songs",
    default=False,
)
@click.option(
    "--wrapper-decrypt-ip",
    type=str,
    default=base_downloader_sig.parameters["wrapper_decrypt_ip"].default,
    help="IP address and port for wrapper decryption",
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
@click.option(
    "--use-album-date",
    is_flag=True,
    help="Use album release date for songs",
    default=song_downloader_sig.parameters["use_album_date"].default,
)
@click.option(
    "--fetch-extra-tags",
    is_flag=True,
    help="Fetch extra tags from preview (normalization and smooth playback)",
    default=song_downloader_sig.parameters["fetch_extra_tags"].default,
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
@click.option(
    "--search",
    is_flag=True,
    help="Search mode (uses iTunes API)",
)
@click.option(
    "--limit",
    type=int,
    default=10,
    help="Number of search results",
)
@click.option(
    "--download",
    is_flag=True,
    help="Download search results (interactive)",
)
@click.option(
    "--json",
    is_flag=True,
    help="Output search results as JSON",
)
@click.option(
    "--input-csv",
    type=click.Path(file_okay=True, dir_okay=False, readable=True, resolve_path=True),
    default=None,
    help="Path to CSV file with title and artist columns",
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
    wrapper_account_url: str,
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
    amdecrypt_path: str,
    use_wrapper: bool,
    wrapper_decrypt_ip: str,
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
    use_album_date: bool,
    fetch_extra_tags: bool,
    music_video_codec_priority: list[MusicVideoCodec],
    music_video_remux_format: RemuxFormatMusicVideo,
    music_video_resolution: MusicVideoResolution,
    uploaded_video_quality: UploadedVideoQuality,
    search: bool,
    limit: int,
    download: bool,
    json: bool,
    input_csv: str,
    *args,
    **kwargs,
):
    colorama.just_fix_windows_console()

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

    if search:
        from rich.console import Console
        from InquirerPy import inquirer
        from InquirerPy.base.control import Choice
        import json as json_lib

        console = Console()
        itunes_api = ItunesApi(storefront="us", language=language)
        query = " ".join(urls)
        
        logger.info(f'Searching for "{query}"...')
        results = await itunes_api.search(query, limit=limit)
        
        # Add constructed song URL to results
        if results.get("results"):
            for item in results["results"]:
                track_id = item.get("trackId")
                collection_view_url = item.get("collectionViewUrl")
                song_url = ""
                
                if track_id and item.get("kind") == "song" and collection_view_url:
                    try:
                        base_url = collection_view_url.split("?")[0]
                        base_url = base_url.replace("/album/", "/song/")
                        parts = base_url.split("/")
                        if parts and parts[-1].isdigit():
                            parts[-1] = str(track_id)
                            song_url = "/".join(parts)
                    except Exception:
                        pass
                
                if not song_url:
                    if track_id and item.get("kind") == "song":
                        song_url = f"https://music.apple.com/{itunes_api.storefront}/song/{track_id}"
                    else:
                        song_url = item.get("trackViewUrl", item.get("collectionViewUrl", ""))
                
                item["songUrl"] = song_url

        if json:
            print(json_lib.dumps(results, indent=4))
            return

        if not results.get("results"):
            logger.info("No results found.")
            return

        choices = []
        for i, item in enumerate(results["results"], 1):
            kind = item.get("kind", "unknown")
            artist = item.get("artistName", "Unknown")
            title = item.get("trackName", item.get("collectionName", "Unknown"))
            album = item.get("collectionName", "")
            
            url = item.get("songUrl")
            
            console.print(f"\n[bold cyan]{i}. {title}[/bold cyan]")
            console.print(f"   Artist: [green]{artist}[/green]")
            console.print(f"   Album:  [yellow]{album}[/yellow]")
            console.print(f"   Type:   {kind}")
            console.print(f"   URL:    [blue]{url}[/blue]")

            choices.append(
                Choice(
                    value=url,
                    name=f"{artist} - {title} ({kind})",
                    enabled=False,
                )
            )

        if not download:
            return
            
        selected_urls = await inquirer.checkbox(
            message="Select items to download:",
            choices=choices,
            validate=lambda result: len(result) >= 1,
            invalid_message="should be at least 1 selection",
            instruction="(Space to select, Enter to confirm)",
        ).execute_async()
        
        urls = selected_urls

    if use_wrapper:
        apple_music_api = await AppleMusicApi.create_from_wrapper(
            wrapper_account_url=wrapper_account_url,
            language=language,
        )
    else:
        cookies_path = prompt_path(cookies_path)
        apple_music_api = await AppleMusicApi.create_from_netscape_cookies(
            cookies_path=cookies_path,
            language=language,
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
        amdecrypt_path=amdecrypt_path,
        use_wrapper=use_wrapper,
        wrapper_decrypt_ip=wrapper_decrypt_ip,
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
    song_downloader = AppleMusicSongDownloader(
        base_downloader=base_downloader,
        interface=song_interface,
        codec=song_codec,
        synced_lyrics_format=synced_lyrics_format,
        no_synced_lyrics=no_synced_lyrics,
        synced_lyrics_only=synced_lyrics_only,
        use_album_date=use_album_date,
        fetch_extra_tags=fetch_extra_tags,
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

        if not base_downloader.full_mp4decrypt_path and (
            song_codec not in (SongCodec.AAC_LEGACY, SongCodec.AAC_HE_LEGACY)
            or remux_mode == RemuxMode.MP4BOX
        ):
            logger.critical(X_NOT_IN_PATH.format("mp4decrypt", mp4decrypt_path))
            return

        if (
            download_mode == DownloadMode.NM3U8DLRE
            and not base_downloader.full_nm3u8dlre_path
        ):
            logger.critical(X_NOT_IN_PATH.format("N_m3u8DL-RE", nm3u8dlre_path))
            return

        if use_wrapper and not base_downloader.full_amdecrypt_path:
            logger.critical(X_NOT_IN_PATH.format("amdecrypt", amdecrypt_path))
            return

        if not song_codec.is_legacy() and not use_wrapper:
            logger.warning(
                "You have chosen an experimental song codec"
                " without enabling wrapper."
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
                    exc_info=not no_exceptions,
                )
                continue

    # Read CSV rows and process in batches: search batch -> download batch -> repeat
    csv_rows_to_process = []
    if input_csv:
        logger.info(f'Reading songs from "{input_csv}"...')
        with open(input_csv, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                reader.fieldnames = [name.strip() for name in reader.fieldnames]
            for row in reader:
                title = row.get("title", "").strip()
                artist = row.get("artist", "").strip()
                if title and artist:
                    csv_rows_to_process.append((title, artist))
        logger.info(f"Found {len(csv_rows_to_process)} songs to process (batch size: {CSV_BATCH_SIZE})")

    if csv_rows_to_process:
        csv_itunes_api = ItunesApi(storefront="us", language=language)
        total_rows = len(csv_rows_to_process)
        total_batches = (total_rows + CSV_BATCH_SIZE - 1) // CSV_BATCH_SIZE
        total_csv_found = 0
        
        for batch_start in range(0, total_rows, CSV_BATCH_SIZE):
            batch_end = min(batch_start + CSV_BATCH_SIZE, total_rows)
            batch_num = (batch_start // CSV_BATCH_SIZE) + 1
            
            logger.info(f"[CSV Batch {batch_num}/{total_batches}] Searching songs {batch_start + 1}-{batch_end}...")
            
            batch_urls = []
            for idx in range(batch_start, batch_end):
                title, artist = csv_rows_to_process[idx]
                query = f"{title} {artist}"
                logger.debug(f'Searching for "{query}"...')
                
                # Search with rate limit retry logic
                results = None
                for retry in range(CSV_MAX_RETRIES):
                    try:
                        results = await csv_itunes_api.search(query, limit=limit)
                        break
                    except Exception as e:
                        error_str = str(e)
                        if "429" in error_str or "rate limit" in error_str.lower():
                            if retry < CSV_MAX_RETRIES - 1:
                                logger.warning(
                                    f"Rate limited. Waiting {CSV_RATE_LIMIT_RETRY_SECONDS}s before retry "
                                    f"({retry + 1}/{CSV_MAX_RETRIES})..."
                                )
                                await asyncio.sleep(CSV_RATE_LIMIT_RETRY_SECONDS)
                            else:
                                logger.error(f"Rate limit exceeded after {CSV_MAX_RETRIES} retries for {title} - {artist}")
                        else:
                            logger.error(f"Error searching for {title} - {artist}: {e}")
                            break
                
                if not results:
                    continue
                
                match = None
                for item in results.get("results", []):
                    item_title = item.get("trackName", "").strip()
                    item_artist = item.get("artistName", "").strip()
                    
                    # Title match (flexible)
                    csv_title_lower = title.lower()
                    api_title_lower = item_title.lower()
                    title_match = (
                        api_title_lower == csv_title_lower or
                        api_title_lower.startswith(csv_title_lower + " (") or
                        api_title_lower.startswith(csv_title_lower + " [") or
                        csv_title_lower.startswith(api_title_lower + " (") or
                        csv_title_lower.startswith(api_title_lower + " [")
                    )
                    if not title_match:
                        continue

                    # Artist match (flexible)
                    csv_artist_lower = artist.lower()
                    api_artist_lower = item_artist.lower()
                    artist_match = False
                    split_pattern = r'[,&]|\s+(?:featuring|feat\.?|ft\.?)\s+'
                    
                    if api_artist_lower == csv_artist_lower:
                        artist_match = True
                    else:
                        csv_parts = [p.strip() for p in re.split(split_pattern, csv_artist_lower, flags=re.IGNORECASE) if p.strip()]
                        if api_artist_lower in csv_parts:
                            artist_match = True
                        if not artist_match:
                            api_parts = [p.strip() for p in re.split(split_pattern, api_artist_lower, flags=re.IGNORECASE) if p.strip()]
                            if csv_artist_lower in api_parts:
                                artist_match = True
                        if not artist_match and len(csv_parts) > 1:
                            api_parts = [p.strip() for p in re.split(split_pattern, api_artist_lower, flags=re.IGNORECASE) if p.strip()]
                            if any(csv_part in api_parts for csv_part in csv_parts):
                                artist_match = True
                    
                    if artist_match:
                        match = item
                        break
                
                if match:
                    track_id = match.get("trackId")
                    collection_view_url = match.get("collectionViewUrl")
                    track_url = ""

                    if track_id:
                        if match.get("kind") == "song" and collection_view_url:
                            try:
                                base_url = collection_view_url.split("?")[0]
                                base_url = base_url.replace("/album/", "/song/")
                                parts = base_url.split("/")
                                if parts and parts[-1].isdigit():
                                    parts[-1] = str(track_id)
                                    track_url = "/".join(parts)
                            except Exception:
                                pass
                        
                        if not track_url:
                            track_url = f"https://music.apple.com/{csv_itunes_api.storefront}/song/{track_id}"
                            
                        logger.info(f"Found match: {title} - {artist}")
                        batch_urls.append(track_url)
                        total_csv_found += 1
                    else:
                        logger.warning(f"Match found but no ID for {title} - {artist}")
                else:
                    logger.warning(f"No exact match found for {title} - {artist}")
            
            # Download this batch immediately
            if batch_urls:
                logger.info(f"[CSV Batch {batch_num}/{total_batches}] Downloading {len(batch_urls)} songs...")
                for url_index, url in enumerate(batch_urls, 1):
                    url_progress = click.style(f"[CSV {batch_num}/{total_batches} - {url_index}/{len(batch_urls)}]", dim=True)
                    logger.info(url_progress + f' Processing "{url}"')
                    download_queue = None
                    try:
                        url_info = downloader.get_url_info(url)
                        if not url_info:
                            logger.warning(url_progress + f' Could not parse "{url}", skipping.')
                            continue
                        download_queue = await downloader.get_download_queue(url_info)
                        if not download_queue:
                            logger.warning(url_progress + f' No downloadable media found for "{url}", skipping.')
                            continue
                    except KeyboardInterrupt:
                        exit(1)
                    except Exception as e:
                        error_count += 1
                        logger.error(url_progress + f' Error processing "{url}"', exc_info=not no_exceptions)

                    if download_queue:
                        for item in download_queue:
                            try:
                                await downloader.download(item)
                            except GamdlError:
                                pass
                            except KeyboardInterrupt:
                                exit(1)
                            except Exception:
                                error_count += 1
                                logger.error(url_progress + " Error downloading", exc_info=not no_exceptions)
            
            # Delay between batches (except after last batch)
            if batch_end < total_rows:
                logger.debug(f"Batch complete. Waiting {CSV_BATCH_DELAY_SECONDS}s before next batch...")
                await asyncio.sleep(CSV_BATCH_DELAY_SECONDS)
        
        logger.info(f"CSV processing complete. Found and downloaded {total_csv_found} of {total_rows} songs.")

    logger.info(f"Finished with {error_count} error(s)")
