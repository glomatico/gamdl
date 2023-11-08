from __future__ import annotations

import json
import logging
from pathlib import Path

import click

from . import __version__
from .constants import *
from .downloader import Downloader


def write_default_config_file(ctx: click.Context) -> None:
    ctx.params["config_location"].parent.mkdir(parents=True, exist_ok=True)
    config_file = {
        param.name: param.default
        for param in ctx.command.params
        if param.name not in EXCLUDED_CONFIG_FILE_PARAMS
    }
    with open(ctx.params["config_location"], "w") as f:
        f.write(json.dumps(config_file, indent=4))


def no_config_callback(
    ctx: click.Context, param: click.Parameter, no_config_file: bool
) -> click.Context:
    if no_config_file:
        return ctx
    if not ctx.params["config_location"].exists():
        write_default_config_file(ctx)
    with open(ctx.params["config_location"], "r") as f:
        config_file = dict(json.load(f))
    for param in ctx.command.params:
        if (
            config_file.get(param.name) is not None
            and not ctx.get_parameter_source(param.name)
            == click.core.ParameterSource.COMMANDLINE
        ):
            ctx.params[param.name] = param.type_cast_value(ctx, config_file[param.name])
    return ctx


@click.command()
@click.argument(
    "urls",
    nargs=-1,
    type=str,
    required=True,
)
@click.option(
    "--final-path",
    "-f",
    type=Path,
    default="./Apple Music",
    help="Path where the downloaded files will be saved.",
)
@click.option(
    "--temp-path",
    "-t",
    type=Path,
    default="./temp",
    help="Path where the temporary files will be saved.",
)
@click.option(
    "--cookies-location",
    "-c",
    type=Path,
    default="./cookies.txt",
    help="Location of the cookies file.",
)
@click.option(
    "--wvd-location",
    "-w",
    type=Path,
    default="./device.wvd",
    help="Location of the .wvd file.",
)
@click.option(
    "--ffmpeg-location",
    type=str,
    default="ffmpeg",
    help="Location of the FFmpeg binary.",
)
@click.option(
    "--mp4box-location",
    type=str,
    default="MP4Box",
    help="Location of the MP4Box binary.",
)
@click.option(
    "--mp4decrypt-location",
    type=str,
    default="mp4decrypt",
    help="Location of the mp4decrypt binary.",
)
@click.option(
    "--nm3u8dlre-location",
    type=str,
    default="N_m3u8DL-RE",
    help="Location of the N_m3u8DL-RE binary.",
)
@click.option(
    "--config-location",
    type=Path,
    default=Path.home() / ".gamdl" / "config.json",
    help="Location of the config file.",
)
@click.option(
    "--template-folder-album",
    type=str,
    default="{album_artist}/{album}",
    help="Template of the album folders as a format string.",
)
@click.option(
    "--template-folder-compilation",
    type=str,
    default="Compilations/{album}",
    help="Template of the compilation album folders as a format string.",
)
@click.option(
    "--template-file-single-disc",
    type=str,
    default="{track:02d} {title}",
    help="Template of the track files for single-disc albums as a format string.",
)
@click.option(
    "--template-file-multi-disc",
    type=str,
    default="{disc}-{track:02d} {title}",
    help="Template of the track files for multi-disc albums as a format string.",
)
@click.option(
    "--template-folder-music-video",
    type=str,
    default="{artist}/Unknown Album",
    help="Template of the music video folders as a format string.",
)
@click.option(
    "--template-file-music-video",
    type=str,
    default="{title}",
    help="Template of the music video files as a format string.",
)
@click.option(
    "--cover-size",
    type=int,
    default=1200,
    help="Size of the cover.",
)
@click.option(
    "--cover-format",
    type=click.Choice(["jpg", "png"]),
    default="jpg",
    help="Format of the cover.",
)
@click.option(
    "--remux-mode",
    type=click.Choice(["ffmpeg", "mp4box"]),
    default="ffmpeg",
    help="Remux mode.",
)
@click.option(
    "--download-mode",
    type=click.Choice(["ytdlp", "nm3u8dlre"]),
    default="ytdlp",
    help="Download mode.",
)
@click.option(
    "--exclude-tags",
    "-e",
    type=str,
    default=None,
    help="List of tags to exclude from file tagging separated by commas.",
)
@click.option(
    "--truncate",
    type=int,
    default=40,
    help="Maximum length of the file/folder names.",
)
@click.option(
    "--log-level",
    "-l",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
    default="INFO",
    help="Log level.",
)
@click.option(
    "--prefer-hevc",
    is_flag=True,
    help="Prefer HEVC over AVC when downloading music videos.",
)
@click.option(
    "--ask-video-format",
    is_flag=True,
    help="Ask for the video format when downloading music videos.",
)
@click.option(
    "--disable-music-video-skip",
    is_flag=True,
    help="Don't skip downloading music videos in albums/playlists.",
)
@click.option(
    "--lrc-only",
    "-l",
    is_flag=True,
    help="Download only the synced lyrics.",
)
@click.option(
    "--no-lrc",
    "-n",
    is_flag=True,
    help="Don't download the synced lyrics.",
)
@click.option(
    "--save-cover",
    "-s",
    is_flag=True,
    help="Save cover as a separate file.",
)
@click.option(
    "--songs-heaac",
    is_flag=True,
    help="Download songs in HE-AAC 64kbps.",
)
@click.option(
    "--overwrite",
    "-o",
    is_flag=True,
    help="Overwrite existing files.",
)
@click.option(
    "--print-exceptions",
    is_flag=True,
    help="Print exceptions.",
)
@click.option(
    "--url-txt",
    "-u",
    is_flag=True,
    help="Read URLs as location of text files containing URLs.",
)
@click.option(
    "--no-config-file",
    "-n",
    is_flag=True,
    callback=no_config_callback,
    help="Don't use the config file.",
)
@click.version_option(__version__, "-v", "--version")
@click.help_option("-h", "--help")
def main(
    urls: tuple[str],
    final_path: Path,
    temp_path: Path,
    cookies_location: Path,
    wvd_location: Path,
    ffmpeg_location: Path,
    mp4box_location: Path,
    mp4decrypt_location: Path,
    nm3u8dlre_location: Path,
    config_location: Path,
    template_folder_album: str,
    template_folder_compilation: str,
    template_file_single_disc: str,
    template_file_multi_disc: str,
    template_folder_music_video: str,
    template_file_music_video: str,
    cover_size: int,
    cover_format: str,
    remux_mode: str,
    download_mode: str,
    exclude_tags: str,
    truncate: int,
    log_level: str,
    prefer_hevc: bool,
    ask_video_format: bool,
    disable_music_video_skip: bool,
    lrc_only: bool,
    no_lrc: bool,
    save_cover: bool,
    songs_heaac: bool,
    overwrite: bool,
    print_exceptions: bool,
    url_txt: bool,
    no_config_file: bool,
):
    logging.basicConfig(
        format="[%(levelname)-8s %(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger(__name__)
    logger.setLevel(log_level)
    logger.debug("Starting downloader")
    downloader = Downloader(**locals())
    if not cookies_location.exists():
        logger.critical(X_NOT_FOUND_STRING.format("Cookies file", cookies_location))
        return
    if remux_mode == "ffmpeg" and not lrc_only:
        if not downloader.ffmpeg_location:
            logger.critical(X_NOT_FOUND_STRING.format("FFmpeg", ffmpeg_location))
            return
        if not downloader.mp4decrypt_location:
            logger.warning(
                X_NOT_FOUND_STRING.format("mp4decrypt", mp4decrypt_location)
                + ", music videos videos will not be downloaded"
            )
    if remux_mode == "mp4box" and not lrc_only:
        if not downloader.mp4box_location:
            logger.critical(X_NOT_FOUND_STRING.format("MP4Box", mp4box_location))
            return
        if not downloader.mp4decrypt_location:
            logger.critical(
                X_NOT_FOUND_STRING.format("mp4decrypt", mp4decrypt_location)
            )
            return
    if download_mode == "nm3u8dlre" and not lrc_only:
        if not downloader.nm3u8dlre_location:
            logger.critical(
                X_NOT_FOUND_STRING.format("N_m3u8DL-RE", nm3u8dlre_location)
            )
            return
        if not downloader.ffmpeg_location:
            logger.critical(X_NOT_FOUND_STRING.format("FFmpeg", ffmpeg_location))
            return
    logger.debug("Setting up session")
    downloader.setup_session()
    if not lrc_only:
        if not wvd_location.exists():
            logger.critical(X_NOT_FOUND_STRING.format(".wvd file", wvd_location))
            return
        logger.debug("Setting up CDM")
        downloader.setup_cdm()
    error_count = 0
    download_queue = []
    if url_txt:
        logger.debug("Reading URLs from text files")
        _urls = []
        for url in urls:
            with open(url, "r") as f:
                _urls.extend(f.read().splitlines())
        urls = tuple(_urls)
    for url_index, url in enumerate(urls, start=1):
        current_url = f"URL {url_index}/{len(urls)}"
        try:
            logger.debug(f'({current_url}) Checking "{url}"')
            download_queue.append(downloader.get_download_queue(url))
        except Exception:
            error_count += 1
            logger.error(
                f'({current_url}) Failed to check "{url}"',
                exc_info=print_exceptions,
            )
    for queue_item_index, queue_item in enumerate(download_queue, start=1):
        download_type, tracks = queue_item
        for track_index, track in enumerate(tracks, start=1):
            current_track = f"Track {track_index}/{len(tracks)} from URL {queue_item_index}/{len(download_queue)}"
            try:
                logger.info(
                    f'({current_track}) Downloading "{track["attributes"]["name"]}"'
                )
                if not track["attributes"].get("playParams"):
                    logger.warning(
                        f"({current_track}) Track is not streamable, skipping"
                    )
                    continue
                track_id = track["id"]
                logger.debug("Getting webplayback")
                webplayback = downloader.get_webplayback(track_id)
                cover_url = downloader.get_cover_url(webplayback)
                if track["type"] == "songs":
                    if track["attributes"]["hasLyrics"]:
                        logger.debug("Getting lyrics")
                        lyrics_unsynced, lyrics_synced = downloader.get_lyrics(
                            track_id,
                        )
                    else:
                        lyrics_unsynced, lyrics_synced = None, None
                    logger.debug("Getting tags")
                    tags = downloader.get_tags_song(webplayback, lyrics_unsynced)
                    final_location = downloader.get_final_location(tags)
                    lrc_location = downloader.get_lrc_location(final_location)
                    cover_location = downloader.get_cover_location_song(final_location)
                    logger.debug(f'Final location is "{final_location}"')
                    if lrc_only:
                        pass
                    elif final_location.exists() and not overwrite:
                        logger.warning(
                            f'({current_track}) Track already exists at "{final_location}", skipping'
                        )
                    else:
                        logger.debug("Getting stream URL")
                        stream_url = downloader.get_stream_url_song(webplayback)
                        logger.debug("Getting decryption key")
                        decryption_key = downloader.get_decryption_key_song(
                            stream_url, track_id
                        )
                        encrypted_location = downloader.get_encrypted_location_audio(
                            track_id
                        )
                        logger.debug(f'Downloading to "{encrypted_location}"')
                        if download_mode == "ytdlp":
                            downloader.download_ytdlp(encrypted_location, stream_url)
                        if download_mode == "nm3u8dlre":
                            downloader.download_nm3u8dlre(
                                encrypted_location, stream_url
                            )
                        decrypted_location = downloader.get_decrypted_location_audio(
                            track_id
                        )
                        fixed_location = downloader.get_fixed_location(track_id, ".m4a")
                        if remux_mode == "ffmpeg":
                            logger.debug(
                                f'Decrypting and remuxing to "{fixed_location}"'
                            )
                            downloader.fixup_song_ffmpeg(
                                encrypted_location, decryption_key, fixed_location
                            )
                        if remux_mode == "mp4box":
                            logger.debug(f'Decrypting to "{decrypted_location}"')
                            downloader.decrypt(
                                encrypted_location,
                                decrypted_location,
                                decryption_key,
                            )
                            logger.debug(f'Remuxing to "{fixed_location}"')
                            downloader.fixup_song_mp4box(
                                decrypted_location, fixed_location
                            )
                        logger.debug("Applying tags")
                        downloader.apply_tags(fixed_location, tags, cover_url)
                        logger.debug("Moving to final location")
                        downloader.move_to_final_location(
                            fixed_location, final_location
                        )
                    if no_lrc or not lyrics_synced:
                        pass
                    elif lrc_location.exists() and not overwrite:
                        logger.debug(
                            f'Synced lyrics already exists at "{lrc_location}", skipping'
                        )
                    else:
                        logger.debug(f'Saving synced lyrics to "{lrc_location}"')
                        downloader.save_lrc(lrc_location, lyrics_synced)
                    if not save_cover or lrc_only:
                        pass
                    elif cover_location.exists() and not overwrite:
                        logger.debug(
                            f'Cover already exists at "{cover_location}", skipping'
                        )
                    else:
                        logger.debug(f'Saving cover to "{cover_location}"')
                        downloader.save_cover(cover_location, cover_url)
                if track["type"] == "music-videos":
                    if (
                        not disable_music_video_skip
                        and download_type in ("album", "playlist")
                        or lrc_only
                        or not downloader.mp4decrypt_location
                    ):
                        logger.warning(
                            f"({current_track}) Music video is not downloadable with current settings, skipping"
                        )
                        continue
                    tags = downloader.get_tags_music_video(
                        track["attributes"]["url"].split("/")[-1].split("?")[0]
                    )
                    final_location = downloader.get_final_location(tags)
                    cover_location = downloader.get_cover_location_music_video(
                        final_location
                    )
                    logger.debug(f'Final location is "{final_location}"')
                    if final_location.exists() and not overwrite:
                        logger.warning(
                            f'({current_track}) Music video already exists at "{final_location}", skipping'
                        )
                    else:
                        logger.debug("Getting stream URLs")
                        (
                            stream_url_video,
                            stream_url_audio,
                        ) = downloader.get_stream_url_music_video(webplayback)
                        logger.debug("Getting decryption keys")
                        decryption_key_video = (
                            downloader.get_decryption_key_music_video(
                                stream_url_video, track_id
                            )
                        )
                        decryption_key_audio = (
                            downloader.get_decryption_key_music_video(
                                stream_url_audio, track_id
                            )
                        )
                        encrypted_location_video = (
                            downloader.get_encrypted_location_video(track_id)
                        )
                        encrypted_location_audio = (
                            downloader.get_encrypted_location_audio(track_id)
                        )
                        decrypted_location_video = (
                            downloader.get_decrypted_location_video(track_id)
                        )
                        decrypted_location_audio = (
                            downloader.get_decrypted_location_audio(track_id)
                        )
                        logger.debug(
                            f'Downloading video to "{encrypted_location_video}"'
                        )
                        if download_mode == "ytdlp":
                            downloader.download_ytdlp(
                                encrypted_location_video, stream_url_video
                            )
                        if download_mode == "nm3u8dlre":
                            downloader.download_nm3u8dlre(
                                encrypted_location_video, stream_url_video
                            )
                        logger.debug(
                            f'Downloading audio to "{encrypted_location_audio}"'
                        )
                        if download_mode == "ytdlp":
                            downloader.download_ytdlp(
                                encrypted_location_audio, stream_url_audio
                            )
                        if download_mode == "nm3u8dlre":
                            downloader.download_nm3u8dlre(
                                encrypted_location_audio, stream_url_audio
                            )
                        logger.debug(
                            f'Decrypting video to "{decrypted_location_video}"'
                        )
                        downloader.decrypt(
                            encrypted_location_audio,
                            decrypted_location_audio,
                            decryption_key_audio,
                        )
                        logger.debug(
                            f'Decrypting audio to "{decrypted_location_audio}"'
                        )
                        downloader.decrypt(
                            encrypted_location_video,
                            decrypted_location_video,
                            decryption_key_video,
                        )
                        fixed_location = downloader.get_fixed_location(track_id, ".m4v")
                        logger.debug(f'Remuxing to "{fixed_location}"')
                        if remux_mode == "ffmpeg":
                            downloader.fixup_music_video_ffmpeg(
                                decrypted_location_video,
                                decrypted_location_audio,
                                fixed_location,
                            )
                        if remux_mode == "mp4box":
                            downloader.fixup_music_video_mp4box(
                                decrypted_location_audio,
                                decrypted_location_video,
                                fixed_location,
                            )
                        logger.debug("Applying tags")
                        downloader.apply_tags(fixed_location, tags, cover_url)
                        logger.debug("Moving to final location")
                        downloader.move_to_final_location(
                            fixed_location, final_location
                        )
                    if not save_cover:
                        pass
                    elif cover_location.exists() and not overwrite:
                        logger.debug(
                            f'Cover already exists at "{cover_location}", skipping'
                        )
                    else:
                        logger.debug(f'Saving cover to "{cover_location}"')
                        downloader.save_cover(cover_location, cover_url)
            except Exception:
                error_count += 1
                logger.error(
                    f'({current_track}) Failed to download "{track["attributes"]["name"]}"',
                    exc_info=print_exceptions,
                )
            finally:
                if temp_path.exists():
                    logger.debug(f'Cleaning up "{temp_path}"')
                    downloader.cleanup_temp_path()
    logger.info(f"Done ({error_count} error(s))")
