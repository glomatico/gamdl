from __future__ import annotations

import inspect
import logging
import typing
from pathlib import Path

import click
import colorama

from . import __version__
from .apple_music_api import AppleMusicApi
from .config_file import ConfigFile
from .constants import *
from .custom_logger_formatter import CustomLoggerFormatter
from .downloader import Downloader
from .downloader_music_video import DownloaderMusicVideo
from .downloader_post import DownloaderPost
from .downloader_song import DownloaderSong
from .enums import (
    CoverFormat,
    DownloadMode,
    MusicVideoCodec,
    MusicVideoResolution,
    PostQuality,
    RemuxFormatMusicVideo,
    RemuxMode,
    SongCodec,
    SyncedLyricsFormat,
)
from .exceptions import *
from .itunes_api import ItunesApi
from .utils import color_text, prompt_path

apple_music_api_from_netscape_cookies_sig = inspect.signature(
    AppleMusicApi.from_netscape_cookies
)
downloader_sig = inspect.signature(Downloader.__init__)
downloader_song_sig = inspect.signature(DownloaderSong.__init__)
downloader_music_video_sig = inspect.signature(DownloaderMusicVideo.__init__)
downloader_post_sig = inspect.signature(DownloaderPost.__init__)

logger = logging.getLogger("gamdl")


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


def load_config_file(
    ctx: click.Context,
    param: click.Parameter,
    no_config_file: bool,
) -> click.Context:
    if no_config_file:
        return ctx

    filtered_params = [
        param
        for param in ctx.command.params
        if param.name not in EXCLUDED_CONFIG_FILE_PARAMS
    ]

    config_file = ConfigFile(ctx.params["config_path"])
    config_file.add_params_default_to_config(
        filtered_params,
    )
    parsed_params = config_file.parse_params_from_config(
        [
            param
            for param in filtered_params
            if ctx.get_parameter_source(param.name)
            != click.core.ParameterSource.COMMANDLINE
        ]
    )
    ctx.params.update(parsed_params)

    return ctx


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
    "--disable-music-video-skip",
    is_flag=True,
    help="Don't skip downloading music videos in albums/playlists.",
)
@click.option(
    "--read-urls-as-txt",
    "-r",
    is_flag=True,
    help="Interpret URLs as paths to text files containing URLs separated by newlines",
)
@click.option(
    "--config-path",
    type=Path,
    default=Path.home() / ".gamdl" / "config.ini",
    help="Path to config file.",
)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
    default="INFO",
    help="Log level.",
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
    type=Path,
    default=apple_music_api_from_netscape_cookies_sig.parameters[
        "cookies_path"
    ].default,
    help="Path to .txt cookies file.",
)
@click.option(
    "--language",
    "-l",
    type=str,
    default=apple_music_api_from_netscape_cookies_sig.parameters["language"].default,
    help="Metadata language as an ISO-2A language code (don't always work for videos).",
)
# Downloader specific options
@click.option(
    "--output-path",
    "-o",
    type=Path,
    default=downloader_sig.parameters["output_path"].default,
    help="Path to output directory.",
)
@click.option(
    "--temp-path",
    type=Path,
    default=downloader_sig.parameters["temp_path"].default,
    help="Path to temporary directory.",
)
@click.option(
    "--wvd-path",
    type=Path,
    default=downloader_sig.parameters["wvd_path"].default,
    help="Path to .wvd file.",
)
@click.option(
    "--overwrite",
    is_flag=True,
    help="Overwrite existing files.",
    default=downloader_sig.parameters["overwrite"].default,
)
@click.option(
    "--save-cover",
    "-s",
    is_flag=True,
    help="Save cover as a separate file.",
    default=downloader_sig.parameters["save_cover"].default,
)
@click.option(
    "--save-playlist",
    is_flag=True,
    help="Save a M3U8 playlist file when downloading a playlist.",
    default=downloader_sig.parameters["save_playlist"].default,
)
@click.option(
    "--no-synced-lyrics",
    is_flag=True,
    help="Don't download the synced lyrics.",
    default=downloader_sig.parameters["no_synced_lyrics"].default,
)
@click.option(
    "--synced-lyrics-only",
    is_flag=True,
    help="Download only the synced lyrics.",
    default=downloader_sig.parameters["synced_lyrics_only"].default,
)
@click.option(
    "--nm3u8dlre-path",
    type=str,
    default=downloader_sig.parameters["nm3u8dlre_path"].default,
    help="Path to N_m3u8DL-RE binary.",
)
@click.option(
    "--mp4decrypt-path",
    type=str,
    default=downloader_sig.parameters["mp4decrypt_path"].default,
    help="Path to mp4decrypt binary.",
)
@click.option(
    "--ffmpeg-path",
    type=str,
    default=downloader_sig.parameters["ffmpeg_path"].default,
    help="Path to FFmpeg binary.",
)
@click.option(
    "--mp4box-path",
    type=str,
    default=downloader_sig.parameters["mp4box_path"].default,
    help="Path to MP4Box binary.",
)
@click.option(
    "--download-mode",
    type=DownloadMode,
    default=downloader_sig.parameters["download_mode"].default,
    help="Download mode.",
)
@click.option(
    "--remux-mode",
    type=RemuxMode,
    default=downloader_sig.parameters["remux_mode"].default,
    help="Remux mode.",
)
@click.option(
    "--cover-format",
    type=CoverFormat,
    default=downloader_sig.parameters["cover_format"].default,
    help="Cover format.",
)
@click.option(
    "--template-folder-album",
    type=str,
    default=downloader_sig.parameters["template_folder_album"].default,
    help="Template folder for tracks that are part of an album.",
)
@click.option(
    "--template-folder-compilation",
    type=str,
    default=downloader_sig.parameters["template_folder_compilation"].default,
    help="Template folder for tracks that are part of a compilation album.",
)
@click.option(
    "--template-file-single-disc",
    type=str,
    default=downloader_sig.parameters["template_file_single_disc"].default,
    help="Template file for the tracks that are part of a single-disc album.",
)
@click.option(
    "--template-file-multi-disc",
    type=str,
    default=downloader_sig.parameters["template_file_multi_disc"].default,
    help="Template file for the tracks that are part of a multi-disc album.",
)
@click.option(
    "--template-folder-no-album",
    type=str,
    default=downloader_sig.parameters["template_folder_no_album"].default,
    help="Template folder for the tracks that are not part of an album.",
)
@click.option(
    "--template-file-no-album",
    type=str,
    default=downloader_sig.parameters["template_file_no_album"].default,
    help="Template file for the tracks that are not part of an album.",
)
@click.option(
    "--template-file-playlist",
    type=str,
    default=downloader_sig.parameters["template_file_playlist"].default,
    help="Template file for the M3U8 playlist.",
)
@click.option(
    "--template-date",
    type=str,
    default=downloader_sig.parameters["template_date"].default,
    help="Date tag template.",
)
@click.option(
    "--exclude-tags",
    type=Csv(str),
    default=downloader_sig.parameters["exclude_tags"].default,
    help="Comma-separated tags to exclude.",
)
@click.option(
    "--cover-size",
    type=int,
    default=downloader_sig.parameters["cover_size"].default,
    help="Cover size.",
)
@click.option(
    "--truncate",
    type=int,
    default=downloader_sig.parameters["truncate"].default,
    help="Maximum length of the file/folder names.",
)
@click.option(
    "--database-path",
    type=Path,
    default=downloader_sig.parameters["database_path"].default,
    help="Path to the downloaded media database file.",
)
# DownloaderSong specific options
@click.option(
    "--codec-song",
    type=SongCodec,
    default=downloader_song_sig.parameters["codec"].default,
    help="Song codec.",
)
@click.option(
    "--synced-lyrics-format",
    type=SyncedLyricsFormat,
    default=downloader_song_sig.parameters["synced_lyrics_format"].default,
    help="Synced lyrics format.",
)
# DownloaderMusicVideo specific options
@click.option(
    "--codec-music-video",
    type=Csv(MusicVideoCodec),
    default=downloader_music_video_sig.parameters["codec"].default,
    help="Comma-separated music video codec priority.",
)
@click.option(
    "--remux-format-music-video",
    type=RemuxFormatMusicVideo,
    default=downloader_music_video_sig.parameters["remux_format"].default,
    help="Music video remux format.",
)
@click.option(
    "--resolution",
    type=MusicVideoResolution,
    default=downloader_music_video_sig.parameters["resolution"].default,
    help="Target video resolution for music videos.",
)
# DownloaderPost specific options
@click.option(
    "--quality-post",
    type=PostQuality,
    default=downloader_post_sig.parameters["quality"].default,
    help="Post video quality.",
)
# This option should always be last
@click.option(
    "--no-config-file",
    "-n",
    is_flag=True,
    callback=load_config_file,
    help="Do not use a config file.",
)
def main(
    urls: list[str],
    disable_music_video_skip: bool,
    read_urls_as_txt: bool,
    config_path: Path,
    log_level: str,
    no_exceptions: bool,
    cookies_path: Path,
    language: str,
    output_path: Path,
    temp_path: Path,
    wvd_path: Path,
    overwrite: bool,
    save_cover: bool,
    save_playlist: bool,
    no_synced_lyrics: bool,
    synced_lyrics_only: bool,
    nm3u8dlre_path: str,
    mp4decrypt_path: str,
    ffmpeg_path: str,
    mp4box_path: str,
    download_mode: DownloadMode,
    remux_mode: RemuxMode,
    cover_format: CoverFormat,
    template_folder_album: str,
    template_folder_compilation: str,
    template_file_single_disc: str,
    template_file_multi_disc: str,
    template_folder_no_album: str,
    template_file_no_album: str,
    template_file_playlist: str,
    template_date: str,
    exclude_tags: list[str],
    cover_size: int,
    truncate: int,
    database_path: Path,
    codec_song: SongCodec,
    synced_lyrics_format: SyncedLyricsFormat,
    codec_music_video: list[MusicVideoCodec],
    remux_format_music_video: RemuxFormatMusicVideo,
    resolution: MusicVideoResolution,
    quality_post: PostQuality,
    no_config_file: bool,
):
    colorama.just_fix_windows_console()

    logger.setLevel(log_level)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(CustomLoggerFormatter())
    logger.addHandler(stream_handler)

    cookies_path = prompt_path(True, cookies_path, "Cookies file")
    if wvd_path:
        wvd_path = prompt_path(True, wvd_path, ".wvd file")

    logger.info("Starting Gamdl")
    apple_music_api = AppleMusicApi.from_netscape_cookies(
        cookies_path,
        language,
    )
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

    itunes_api = ItunesApi(
        apple_music_api.storefront,
        apple_music_api.language,
    )

    downloader = Downloader(
        apple_music_api,
        itunes_api,
        output_path,
        temp_path,
        wvd_path,
        overwrite,
        save_cover,
        save_playlist,
        no_synced_lyrics,
        synced_lyrics_only,
        nm3u8dlre_path,
        mp4decrypt_path,
        ffmpeg_path,
        mp4box_path,
        download_mode,
        remux_mode,
        cover_format,
        template_folder_album,
        template_folder_compilation,
        template_file_single_disc,
        template_file_multi_disc,
        template_folder_no_album,
        template_file_no_album,
        template_file_playlist,
        template_date,
        exclude_tags,
        cover_size,
        truncate,
        database_path,
        log_level in ("WARNING", "ERROR"),
    )

    downloader_song = DownloaderSong(
        downloader,
        codec_song,
        synced_lyrics_format,
    )
    downloader_music_video = DownloaderMusicVideo(
        downloader,
        codec_music_video,
        remux_format_music_video,
        resolution,
    )

    downloader_post = DownloaderPost(
        downloader,
        quality_post,
    )

    skip_mv = False

    if not synced_lyrics_only:
        logger.debug("Setting up CDM")
        downloader.set_cdm()

        if not downloader.ffmpeg_path_full and (
            remux_mode == RemuxMode.FFMPEG or download_mode == DownloadMode.NM3U8DLRE
        ):
            logger.critical(X_NOT_FOUND_STRING.format("ffmpeg", ffmpeg_path))
            return

        if not downloader.mp4box_path_full and remux_mode == RemuxMode.MP4BOX:
            logger.critical(X_NOT_FOUND_STRING.format("MP4Box", mp4box_path))
            return

        if (
            not downloader.mp4decrypt_path_full
            and codec_song
            not in (
                SongCodec.AAC_LEGACY,
                SongCodec.AAC_HE_LEGACY,
            )
            or (remux_mode == RemuxMode.MP4BOX and not downloader.mp4decrypt_path_full)
        ):
            logger.critical(X_NOT_FOUND_STRING.format("mp4decrypt", mp4decrypt_path))
            return

        if (
            download_mode == DownloadMode.NM3U8DLRE
            and not downloader.nm3u8dlre_path_full
        ):
            logger.critical(X_NOT_FOUND_STRING.format("N_m3u8DL-RE", nm3u8dlre_path))
            return

        if not downloader.mp4decrypt_path_full:
            logger.warning(
                X_NOT_FOUND_STRING.format("mp4decrypt", mp4decrypt_path)
                + ", music videos will not be downloaded"
            )
            skip_mv = True

        if not codec_song.is_legacy():
            logger.warning(
                "You have chosen an experimental song codec. "
                "They're not guaranteed to work due to API limitations."
            )

    if read_urls_as_txt:
        _urls = []
        for url in urls:
            if Path(url).exists():
                _urls.extend(Path(url).read_text(encoding="utf-8").splitlines())
        urls = _urls

    error_count = 0

    for url_index, url in enumerate(urls, start=1):
        url_progress = color_text(f"URL {url_index}/{len(urls)}", colorama.Style.DIM)
        try:
            logger.info(f'({url_progress}) Processing "{url}"')
            url_info = downloader.parse_url_info(url)

            if not url_info:
                error_count += 1
                logger.error(f"({url_progress}) Invalid URL, skipping")
                continue

            download_queue = downloader.get_download_queue(url_info)

            if not download_queue:
                error_count += 1
                logger.error(f"({url_progress}) Media not found, skipping")
                continue

            download_queue_medias_metadata = download_queue.medias_metadata
        except Exception as e:
            error_count += 1
            logger.error(
                f'({url_progress}) Failed to process URL "{url}", skipping',
                exc_info=not no_exceptions,
            )
            continue
        for download_index, media_metadata in enumerate(
            download_queue_medias_metadata,
            start=1,
        ):
            queue_progress = color_text(
                f"Track {download_index}/{len(download_queue_medias_metadata)} from URL {url_index}/{len(urls)}",
                colorama.Style.DIM,
            )
            try:
                logger.info(
                    f'({queue_progress}) "{media_metadata["attributes"]["name"]}"'
                )

                if (
                    (
                        synced_lyrics_only
                        and media_metadata["type"] not in {"songs", "library-songs"}
                    )
                    or (media_metadata["type"] == "music-videos" and skip_mv)
                    or (
                        media_metadata["type"] == "music-videos"
                        and url_info.type == "album"
                        and not disable_music_video_skip
                    )
                ):
                    logger.warning(
                        f"({queue_progress}) Track is not downloadable with current configuration, skipping"
                    )
                    continue

                if media_metadata["type"] in {"songs", "library-songs"}:
                    for _ in downloader_song.download(
                        media_metadata=media_metadata,
                        playlist_attributes=download_queue.playlist_attributes,
                        playlist_track=download_index,
                    ):
                        pass

                if media_metadata["type"] in {"music-videos", "library-music-videos"}:
                    for _ in downloader_music_video.download(
                        media_metadata=media_metadata,
                        playlist_attributes=download_queue.playlist_attributes,
                        playlist_track=download_index,
                    ):
                        pass

                if media_metadata["type"] == "uploaded-videos":
                    for _ in downloader_post.download(
                        media_metadata=media_metadata,
                    ):
                        pass
            except KeyboardInterrupt:
                exit(0)
            except (
                MediaNotStreamableException,
                MediaFileAlreadyExistsException,
                MediaFormatNotAvailableException,
            ) as e:
                logger.warning(
                    f"({queue_progress}) {e}, skipping",
                )
            except Exception as e:
                error_count += 1
                logger.error(
                    f'({queue_progress}) Failed to download "{media_metadata["attributes"]["name"]}"',
                    exc_info=not no_exceptions,
                )

    logger.info(f"Done, {error_count} error(s) occurred")
