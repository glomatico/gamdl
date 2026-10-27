"""Dataclass-Click option schema for the rip command."""

from dataclasses import dataclass
from typing import Annotated

import click
from dataclass_click import argument, option as dataclass_option

from gamdl.media import MusicVideoCodec, SongCodec, UploadedVideoQuality
from gamdl.media.constants import DEFAULT_RIP_AUTO_SELECT, RIP_AUTO_SELECT_NAMES


def option(*names, **kwargs):
    """Defaults come from dataclasses/Click default_map, not required flags."""
    return dataclass_option(*names, required=False, **kwargs)


@dataclass
class RipOptions:
    urls: Annotated[tuple[str, ...], argument(nargs=-1)]
    auto_select: Annotated[
        tuple[str, ...],
        option(
            "--auto-select",
            multiple=True,
            type=click.Choice(list(RIP_AUTO_SELECT_NAMES)),
            help="Repeat to select album/playlist tracks or artist content automatically",
            show_default=True,
        ),
    ] = DEFAULT_RIP_AUTO_SELECT
    output_dir: Annotated[str, option("--output-dir", help="Destination root")] = (
        "./Apple Music"
    )
    overwrite: Annotated[
        str, option("--overwrite", type=click.Choice(["skip", "replace", "error"]))
    ] = "skip"
    concurrency: Annotated[
        int,
        option(
            "--concurrency",
            type=click.IntRange(min=1),
            help="Parallel requests per media",
        ),
    ] = 8
    concurrent_downloads: Annotated[
        int,
        option(
            "--concurrent-downloads",
            type=click.IntRange(min=1),
            help="Media items in parallel",
        ),
    ] = 2
    song_codec: Annotated[
        str | None,
        option("--song-codec", type=click.Choice([c.value for c in SongCodec])),
    ] = None
    song_codec_priority: Annotated[
        tuple[str, ...],
        option(
            "--song-codec-priority",
            multiple=True,
            type=click.Choice([c.value for c in SongCodec]),
            help="Repeat flag to set song codec priority",
        ),
    ] = ()
    music_video_codec: Annotated[
        str | None,
        option(
            "--music-video-codec", type=click.Choice([c.value for c in MusicVideoCodec])
        ),
    ] = None
    music_video_codec_priority: Annotated[
        tuple[str, ...],
        option(
            "--music-video-codec-priority",
            multiple=True,
            type=click.Choice([c.value for c in MusicVideoCodec]),
        ),
    ] = ()
    max_height: Annotated[int, option("--max-height", type=click.IntRange(min=1))] = (
        1080
    )
    uploaded_video_quality: Annotated[
        str | None,
        option(
            "--uploaded-video-quality",
            type=click.Choice([q.value for q in UploadedVideoQuality]),
        ),
    ] = None
    ask_song_codec: Annotated[
        bool,
        option(
            "--ask-song-codec",
            is_flag=True,
            help="Interactively choose the song stream",
        ),
    ] = False
    ask_music_video_streams: Annotated[
        bool,
        option(
            "--ask-music-video-streams",
            is_flag=True,
            help="Choose music-video audio and video",
        ),
    ] = False
    ask_uploaded_video_quality: Annotated[
        bool,
        option(
            "--ask-uploaded-video-quality",
            is_flag=True,
            help="Choose uploaded-video quality",
        ),
    ] = False
    save_cover: Annotated[
        bool, option("--save-cover/--no-save-cover", help="Save external artwork")
    ] = False
    save_lyrics: Annotated[
        bool, option("--save-lyrics/--no-save-lyrics", help="Save song LRC/text")
    ] = True
    save_playlist: Annotated[
        bool,
        option("--save-playlist/--no-save-playlist", help="Generate M3U for playlists"),
    ] = False
    continue_on_error: Annotated[
        bool,
        option(
            "--continue-on-error/--fail-fast",
            help="Keep downloading after a media error",
        ),
    ] = True
    database: Annotated[
        bool, option("--database/--no-database", help="Use the SQLite media index")
    ] = False
    database_path: Annotated[str | None, option("--database-path", type=str)] = None
    progress: Annotated[
        bool, option("--progress/--no-progress", help="Rich dynamic progress bars")
    ] = True
    max_visible_tracks: Annotated[
        int, option("--max-visible-tracks", type=click.IntRange(min=1))
    ] = 5
    debug: Annotated[
        bool, option("--debug/--no-debug", help="Display unexpected tracebacks")
    ] = False
    auth_mode: Annotated[
        str, option("--auth-mode", type=click.Choice(["cookies", "wrapper"]))
    ] = "cookies"
    cookies_path: Annotated[str | None, option("--cookies-path", type=str)] = None
    wrapper_url: Annotated[str, option("--wrapper-url", type=str)] = "http://127.0.0.1"
    wrapper_decrypt_host: Annotated[str, option("--wrapper-decrypt-host", type=str)] = (
        "127.0.0.1"
    )
    wrapper_decrypt_port: Annotated[
        int, option("--wrapper-decrypt-port", type=click.IntRange(min=1, max=65535))
    ] = 10020
    storefront: Annotated[str | None, option("--storefront", type=str)] = None
    language: Annotated[str, option("--language", type=str)] = "en-US"
    drm_backend: Annotated[
        str, option("--drm-backend", type=click.Choice(["widevine", "playready"]))
    ] = "widevine"
    device_path: Annotated[str | None, option("--device-path", type=str)] = None
    album_folder_template: Annotated[str, option("--album-folder-template")] = (
        "{album_artist}/{album}"
    )
    compilation_folder_template: Annotated[
        str, option("--compilation-folder-template")
    ] = "Compilations/{album}"
    no_album_folder_template: Annotated[str, option("--no-album-folder-template")] = (
        "{artist}/Unknown Album"
    )
    playlist_folder_template: Annotated[str, option("--playlist-folder-template")] = (
        "Playlists/{playlist_artist}"
    )
    single_disc_file_template: Annotated[str, option("--single-disc-file-template")] = (
        "{track:02d} {title}"
    )
    multi_disc_file_template: Annotated[str, option("--multi-disc-file-template")] = (
        "{disc}-{track:02d} {title}"
    )
    no_album_file_template: Annotated[str, option("--no-album-file-template")] = (
        "{title}"
    )
    playlist_file_template: Annotated[str, option("--playlist-file-template")] = (
        "{playlist_title}"
    )
    truncate: Annotated[
        int | None, option("--truncate", type=click.IntRange(min=1))
    ] = None
