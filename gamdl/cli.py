from __future__ import annotations

import inspect
import json
import logging
from enum import Enum
from pathlib import Path

import click

from . import __version__
from .apple_music_api import AppleMusicApi
from .constants import *
from .downloader import Downloader
from .downloader_music_video import DownloaderMusicVideo
from .downloader_post import DownloaderPost
from .downloader_song import DownloaderSong
from .downloader_song_legacy import DownloaderSongLegacy
from .enums import CoverFormat, DownloadMode, MusicVideoCodec, PostQuality, RemuxMode, DRM
from .itunes_api import ItunesApi

apple_music_api_sig = inspect.signature(AppleMusicApi.__init__)
downloader_sig = inspect.signature(Downloader.__init__)
downloader_song_sig = inspect.signature(DownloaderSong.__init__)
downloader_music_video_sig = inspect.signature(DownloaderMusicVideo.__init__)
downloader_post_sig = inspect.signature(DownloaderPost.__init__)


def get_param_string(param: click.Parameter) -> str:
    if isinstance(param.default, Enum):
        return param.default.value
    elif isinstance(param.default, Path):
        return str(param.default)
    else:
        return param.default


def write_default_config_file(ctx: click.Context):
    ctx.params["config_path"].parent.mkdir(parents=True, exist_ok=True)
    config_file = {
        param.name: get_param_string(param)
        for param in ctx.command.params
        if param.name not in EXCLUDED_CONFIG_FILE_PARAMS
    }
    ctx.params["config_path"].write_text(json.dumps(config_file, indent=4))


def load_config_file(
    ctx: click.Context,
    param: click.Parameter,
    no_config_file: bool,
) -> click.Context:
    if no_config_file:
        return ctx
    if not ctx.params["config_path"].exists():
        write_default_config_file(ctx)
    config_file = dict(json.loads(ctx.params["config_path"].read_text()))
    for param in ctx.command.params:
        if (
            config_file.get(param.name) is not None
            and not ctx.get_parameter_source(param.name)
            == click.core.ParameterSource.COMMANDLINE
        ):
            ctx.params[param.name] = param.type_cast_value(ctx, config_file[param.name])
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
    "--save-cover",
    "-s",
    is_flag=True,
    help="Save cover as a separate file.",
)
@click.option(
    "--overwrite",
    is_flag=True,
    help="Overwrite existing files.",
)
@click.option(
    "--read-urls-as-txt",
    "-r",
    is_flag=True,
    help="Interpret URLs as paths to text files containing URLs separated by newlines",
)
@click.option(
    "--save-playlist",
    is_flag=True,
    help="Save a M3U8 playlist file when downloading a playlist.",
)
@click.option(
    "--synced-lyrics-only",
    is_flag=True,
    help="Download only the synced lyrics.",
)
@click.option(
    "--no-synced-lyrics",
    is_flag=True,
    help="Don't download the synced lyrics.",
)
@click.option(
    "--config-path",
    type=Path,
    default=Path.home() / ".gamdl" / "config.json",
    help="Path to config file.",
)
@click.option(
    "--log-level",
    type=str,
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
    default=apple_music_api_sig.parameters["cookies_path"].default,
    help="Path to .txt cookies file.",
)
@click.option(
    "--language",
    "-l",
    type=str,
    default=apple_music_api_sig.parameters["language"].default,
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
    "--device-path",
    type=Path,
    default=downloader_sig.parameters["device_path"].default,
    help="Path to .wvd or .prd file.",
)
@click.option(
    "--playready",
    is_flag=True,
    help="Use PlayReady DRM.",
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
    type=str,
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
    type=MusicVideoCodec,
    default=downloader_music_video_sig.parameters["codec"].default,
    help="Music video codec.",
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
    save_cover: bool,
    overwrite: bool,
    read_urls_as_txt: bool,
    save_playlist: bool,
    synced_lyrics_only: bool,
    no_synced_lyrics: bool,
    config_path: Path,
    log_level: str,
    no_exceptions: bool,
    cookies_path: Path,
    language: str,
    output_path: Path,
    temp_path: Path,
    device_path: Path,
    playready: bool,
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
    exclude_tags: str,
    cover_size: int,
    truncate: int,
    codec_song: SongCodec,
    synced_lyrics_format: SyncedLyricsFormat,
    codec_music_video: MusicVideoCodec,
    quality_post: PostQuality,
    no_config_file: bool,
):
    logging.basicConfig(
        format="[%(levelname)-8s %(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger(__name__)
    logger.setLevel(log_level)
    logger.info("Starting Gamdl")
    if not cookies_path.exists():
        logger.critical(X_NOT_FOUND_STRING.format("Cookies file", cookies_path))
        return
    apple_music_api = AppleMusicApi(
        cookies_path,
        language=language,
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
        device_path,
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
        DRM.Playready if playready else DRM.Widevine
    )
    downloader_song = DownloaderSong(
        downloader,
        codec_song,
        synced_lyrics_format,
    )
    downloader_song_legacy = DownloaderSongLegacy(
        downloader,
        codec_song,
    )
    downloader_music_video = DownloaderMusicVideo(
        downloader,
        codec_music_video,
    )
    downloader_post = DownloaderPost(
        downloader,
        quality_post,
    )
    if not synced_lyrics_only:
        if device_path and not device_path.exists():
            logger.critical(X_NOT_FOUND_STRING.format(".wvd file", device_path))
            return
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
            logger.warn(
                X_NOT_FOUND_STRING.format("mp4decrypt", mp4decrypt_path)
                + ", music videos will not be downloaded"
            )
            skip_mv = True
        else:
            skip_mv = False
        if codec_song not in LEGACY_CODECS:
            logger.warn(
                "You have chosen a non-legacy codec. Support for non-legacy codecs are not guaranteed, "
                "as most of the songs cannot be downloaded when using non-legacy codecs."
            )
    error_count = 0
    if read_urls_as_txt:
        _urls = []
        for url in urls:
            if Path(url).exists():
                _urls.extend(Path(url).read_text(encoding="utf-8").splitlines())
        urls = _urls
    for url_index, url in enumerate(urls, start=1):
        url_progress = f"URL {url_index}/{len(urls)}"
        try:
            logger.info(f'({url_progress}) Checking "{url}"')
            url_info = downloader.get_url_info(url)
            download_queue = downloader.get_download_queue(url_info)
            download_queue_tracks_metadata = download_queue.tracks_metadata
        except Exception as e:
            error_count += 1
            logger.error(
                f'({url_progress}) Failed to check "{url}"',
                exc_info=not no_exceptions,
            )
            continue
        for download_index, track_metadata in enumerate(
            download_queue_tracks_metadata, start=1
        ):
            queue_progress = f"Track {download_index}/{len(download_queue_tracks_metadata)} from URL {url_index}/{len(urls)}"
            try:
                remuxed_path = None
                if download_queue.playlist_attributes:
                    playlist_track = download_index
                else:
                    playlist_track = None
                logger.info(
                    f'({queue_progress}) Downloading "{track_metadata["attributes"]["name"]}"'
                )
                if not track_metadata["attributes"].get("playParams"):
                    logger.warning(
                        f"({queue_progress}) Track is not streamable, skipping"
                    )
                    continue
                if (
                    (synced_lyrics_only and track_metadata["type"] != "songs")
                    or (track_metadata["type"] == "music-videos" and skip_mv)
                    or (
                        track_metadata["type"] == "music-videos"
                        and url_info.type == "album"
                        and not disable_music_video_skip
                    )
                ):
                    logger.warning(
                        f"({queue_progress}) Track is not downloadable with current configuration, skipping"
                    )
                    continue
                elif track_metadata["type"] == "songs":
                    logger.debug("Getting lyrics")
                    lyrics = downloader_song.get_lyrics(track_metadata)
                    logger.debug("Getting webplayback")
                    webplayback = apple_music_api.get_webplayback(track_metadata["id"])
                    logger.debug(webplayback)
                    tags = downloader_song.get_tags(webplayback, lyrics.unsynced)
                    if playlist_track:
                        tags = {
                            **tags,
                            **downloader.get_playlist_tags(
                                download_queue.playlist_attributes,
                                playlist_track,
                            ),
                        }
                    final_path = downloader.get_final_path(tags, ".m4a")
                    lyrics_synced_path = downloader_song.get_lyrics_synced_path(
                        final_path
                    )
                    cover_url = downloader.get_cover_url(track_metadata)
                    cover_file_extesion = downloader.get_cover_file_extension(cover_url)
                    cover_path = downloader_song.get_cover_path(
                        final_path,
                        cover_file_extesion,
                    )
                    if synced_lyrics_only:
                        pass
                    elif final_path.exists() and not overwrite:
                        logger.warning(
                            f'({queue_progress}) Song already exists at "{final_path}", skipping'
                        )
                    else:
                        logger.debug("Getting stream info")
                        if codec_song in LEGACY_CODECS:
                            stream_info = downloader_song_legacy.get_stream_info(
                                webplayback
                            )
                            logger.debug("Getting decryption key")
                            decryption_key = downloader_song_legacy.get_decryption_key(
                                stream_info.pssh, track_metadata["id"]
                            )
                        else:
                            stream_info = downloader_song.get_stream_info(
                                track_metadata
                            )
                            logger.debug(track_metadata)
                            if not stream_info.stream_url or not stream_info.pssh:
                                logger.warning(
                                    f"({queue_progress}) Song is not downloadable or is not"
                                    " available in the chosen codec, skipping"
                                )
                                continue
                            logger.debug(f"{stream_info.pssh}, {stream_info.stream_url}")
                            logger.debug("Getting decryption key")
                            decryption_key = downloader.get_decryption_key(
                                stream_info.pssh, track_metadata["id"]
                            )
                        encrypted_path = downloader_song.get_encrypted_path(
                            track_metadata["id"]
                        )
                        decrypted_path = downloader_song.get_decrypted_path(
                            track_metadata["id"]
                        )
                        remuxed_path = downloader_song.get_remuxed_path(
                            track_metadata["id"]
                        )
                        logger.debug(f'Downloading to "{encrypted_path}"')
                        downloader.download(encrypted_path, stream_info.stream_url)
                        if codec_song in LEGACY_CODECS:
                            logger.debug(
                                f'Decrypting/Remuxing to "{decrypted_path}"/"{remuxed_path}"'
                            )
                            downloader_song_legacy.remux(
                                encrypted_path,
                                decrypted_path,
                                remuxed_path,
                                decryption_key,
                            )
                        else:
                            logger.debug(f'Decrypting to "{decrypted_path}"')
                            downloader_song.decrypt(
                                encrypted_path, decrypted_path, decryption_key
                            )
                            logger.debug(f'Remuxing to "{final_path}"')
                            downloader_song.remux(
                                decrypted_path,
                                remuxed_path,
                                stream_info.codec,
                            )
                    if no_synced_lyrics or not lyrics.synced:
                        pass
                    elif lyrics_synced_path.exists() and not overwrite:
                        logger.debug(
                            f'Synced lyrics already exists at "{lyrics_synced_path}", skipping'
                        )
                    else:
                        logger.debug(f'Saving synced lyrics to "{lyrics_synced_path}"')
                        downloader_song.save_lyrics_synced(
                            lyrics_synced_path, lyrics.synced
                        )
                elif track_metadata["type"] == "music-videos":
                    music_video_id_alt = downloader_music_video.get_music_video_id_alt(
                        track_metadata
                    )
                    logger.debug("Getting iTunes page")
                    itunes_page = itunes_api.get_itunes_page(
                        "music-video", music_video_id_alt
                    )
                    if music_video_id_alt == track_metadata["id"]:
                        stream_url = (
                            downloader_music_video.get_stream_url_from_itunes_page(
                                itunes_page
                            )
                        )
                    else:
                        logger.debug("Getting webplayback")
                        webplayback = apple_music_api.get_webplayback(
                            track_metadata["id"]
                        )
                        stream_url = (
                            downloader_music_video.get_stream_url_from_webplayback(
                                webplayback
                            )
                        )
                    logger.debug("Getting M3U8 data")
                    m3u8_data = downloader_music_video.get_m3u8_master_data(stream_url)
                    tags = downloader_music_video.get_tags(
                        music_video_id_alt,
                        itunes_page,
                        track_metadata,
                    )
                    if playlist_track:
                        tags = {
                            **tags,
                            **downloader.get_playlist_tags(
                                download_queue.playlist_attributes,
                                playlist_track,
                            ),
                        }
                    final_path = downloader.get_final_path(tags, ".m4v")
                    cover_url = downloader.get_cover_url(track_metadata)
                    cover_file_extesion = downloader.get_cover_file_extension(cover_url)
                    cover_path = downloader_music_video.get_cover_path(
                        final_path,
                        cover_file_extesion,
                    )
                    if final_path.exists() and not overwrite:
                        logger.warning(
                            f'({queue_progress}) Music video already exists at "{final_path}", skipping'
                        )
                    else:
                        logger.debug("Getting stream info")
                        stream_info_video, stream_info_audio = (
                            downloader_music_video.get_stream_info_video(m3u8_data),
                            downloader_music_video.get_stream_info_audio(m3u8_data),
                        )
                        decryption_key_video = downloader.get_decryption_key(
                            stream_info_video.pssh, track_metadata["id"]
                        )
                        decryption_key_audio = downloader.get_decryption_key(
                            stream_info_audio.pssh, track_metadata["id"]
                        )
                        encrypted_path_video = (
                            downloader_music_video.get_encrypted_path_video(
                                track_metadata["id"]
                            )
                        )
                        encrypted_path_audio = (
                            downloader_music_video.get_encrypted_path_audio(
                                track_metadata["id"]
                            )
                        )
                        decrypted_path_video = (
                            downloader_music_video.get_decrypted_path_video(
                                track_metadata["id"]
                            )
                        )
                        decrypted_path_audio = (
                            downloader_music_video.get_decrypted_path_audio(
                                track_metadata["id"]
                            )
                        )
                        remuxed_path = downloader_music_video.get_remuxed_path(
                            track_metadata["id"]
                        )
                        logger.debug(f'Downloading video to "{encrypted_path_video}"')
                        downloader.download(
                            encrypted_path_video, stream_info_video.stream_url
                        )
                        logger.debug(f'Downloading audio to "{encrypted_path_audio}"')
                        downloader.download(
                            encrypted_path_audio, stream_info_audio.stream_url
                        )
                        logger.debug(f'Decrypting video to "{decrypted_path_video}"')
                        downloader_music_video.decrypt(
                            encrypted_path_video,
                            decryption_key_video,
                            decrypted_path_video,
                        )
                        logger.debug(f'Decrypting audio to "{decrypted_path_audio}"')
                        downloader_music_video.decrypt(
                            encrypted_path_audio,
                            decryption_key_audio,
                            decrypted_path_audio,
                        )
                        logger.debug(f'Remuxing to "{remuxed_path}"')
                        downloader_music_video.remux(
                            decrypted_path_video,
                            decrypted_path_audio,
                            remuxed_path,
                            stream_info_video.codec,
                            stream_info_audio.codec,
                        )
                elif track_metadata["type"] == "uploaded-videos":
                    stream_url = downloader_post.get_stream_url(track_metadata)
                    tags = downloader_post.get_tags(track_metadata)
                    final_path = downloader.get_final_path(tags, ".m4v")
                    cover_url = downloader.get_cover_url(track_metadata)
                    cover_file_extesion = downloader.get_cover_file_extension(cover_url)
                    cover_path = downloader_music_video.get_cover_path(
                        final_path,
                        cover_file_extesion,
                    )
                    if final_path.exists() and not overwrite:
                        logger.warning(
                            f'({queue_progress}) Post video already exists at "{final_path}", skipping'
                        )
                    else:
                        remuxed_path = downloader_post.get_post_temp_path(
                            track_metadata["id"]
                        )
                        logger.debug(f'Downloading to "{remuxed_path}"')
                        downloader.download_ytdlp(remuxed_path, stream_url)
                if synced_lyrics_only or not save_cover:
                    pass
                elif cover_path.exists() and not overwrite:
                    logger.debug(f'Cover already exists at "{cover_path}", skipping')
                else:
                    logger.debug(f'Saving cover to "{cover_path}"')
                    downloader.save_cover(cover_path, cover_url)
                if remuxed_path:
                    logger.debug("Applying tags")
                    downloader.apply_tags(remuxed_path, tags, cover_url)
                    logger.debug(f'Moving to "{final_path}"')
                    downloader.move_to_output_path(remuxed_path, final_path)
                if (
                    not synced_lyrics_only
                    and save_playlist
                    and download_queue.playlist_attributes
                ):
                    playlist_file_path = downloader.get_playlist_file_path(tags)
                    logger.debug(f'Updating M3U8 playlist from "{playlist_file_path}"')
                    downloader.update_playlist_file(
                        playlist_file_path,
                        final_path,
                        playlist_track,
                    )
            except Exception as e:
                error_count += 1
                logger.error(
                    f'({queue_progress}) Failed to download "{track_metadata["attributes"]["name"]}"',
                    exc_info=not no_exceptions,
                )
            finally:
                if temp_path.exists():
                    logger.debug(f'Cleaning up "{temp_path}"')
                    downloader.cleanup_temp_path()
    logger.info(f"Done ({error_count} error(s))")
