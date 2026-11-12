"""Dataclass-Click option schema for the rip command."""

from collections.abc import Sequence
from dataclasses import dataclass, field, fields, make_dataclass
from typing import Annotated

import click
from dataclass_click import argument
from dataclass_click import option as dataclass_option

from gamdl.download.tags import Tags
from gamdl.media import (
    CoverFormat,
    LyricsFormat,
    MusicVideoCodec,
    SongCodec,
    UploadedVideoQuality,
)

TAG_NAMES = tuple(field.name for field in fields(Tags)) + ("cover",)


def option(*names, **kwargs):
    """Defaults come from dataclasses/Click default_map, not required flags."""
    return dataclass_option(*names, required=False, **kwargs)


def comma_separated_choices(choices: Sequence[str]):
    """Accept one comma-separated value or repeated flags, preserving order."""
    choice_type = click.Choice(choices)

    def convert(ctx: click.Context, param: click.Parameter, values: tuple[str, ...]):
        result = []
        for value in values:
            for item in value.split(","):
                item = item.strip()
                if not item:
                    raise click.BadParameter("Empty entries are not allowed")
                result.append(choice_type.convert(item, param, ctx))
        return tuple(result)

    return convert


@dataclass
class RipOptions:
    urls: Annotated[tuple[str, ...], argument(nargs=-1)] = ()
    select: Annotated[
        bool,
        option(
            "--select",
            is_flag=True,
            help="Interactively select items in all collections",
        ),
    ] = False
    artist_auto_select: Annotated[
        tuple[str, ...],
        option(
            "--artist-auto-select",
            multiple=True,
            type=str,
            help="Artist categories, comma-separated or repeated; all = all-albums,music-videos",
            show_default=True,
        ),
    ] = ()
    output_dir: Annotated[
        str | None,
        option("--output-dir", help="Destination root (prompts if unset)"),
    ] = None
    temp_path: Annotated[
        str | None,
        option("--temp-path", help="Working directory for disk-backed downloads"),
    ] = None
    overwrite: Annotated[
        bool,
        option(
            "--overwrite/--no-overwrite",
            help="Replace existing files instead of skipping them",
        ),
    ] = False
    segment_concurrency: Annotated[
        int,
        option(
            "--segment-concurrency",
            type=click.IntRange(min=1),
            help="Parallel media segment requests per track",
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
            type=str,
            callback=comma_separated_choices([c.value for c in SongCodec]),
            help="Comma-separated song codecs in priority order (or repeat flag)",
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
            type=str,
            callback=comma_separated_choices([c.value for c in MusicVideoCodec]),
            help="Comma-separated video codecs in priority order (or repeat flag)",
        ),
    ] = ()
    music_video_container: Annotated[
        str,
        option(
            "--music-video-container",
            type=click.Choice(["m4v", "mp4"]),
            help="Music-video file extension and MP4 brand (default: m4v)",
        ),
    ] = "m4v"
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
    cover_format: Annotated[
        str,
        option(
            "--cover-format",
            type=click.Choice([item.value for item in CoverFormat]),
            help="Embedded and saved artwork format (jpg or png)",
        ),
    ] = CoverFormat.JPG.value
    no_tag: Annotated[
        bool,
        option(
            "--no-tag/--tag",
            help="Skip or enable writing metadata tags",
        ),
    ] = False
    exclude_tags: Annotated[
        tuple[str, ...],
        option(
            "--exclude-tags",
            multiple=True,
            type=str,
            help="Comma-separated metadata fields to omit (or cover)",
        ),
    ] = ()
    no_lyrics_file: Annotated[
        bool,
        option("--no-lyrics-file", is_flag=True, help="Skip saving external lyrics"),
    ] = False
    lyrics_file_format: Annotated[
        str,
        option(
            "--lyrics-file-format",
            type=click.Choice([item.value for item in LyricsFormat]),
            help="External lyrics format (default: lrc)",
        ),
    ] = LyricsFormat.LRC.value
    embedded_lyrics_format: Annotated[
        str,
        option(
            "--embedded-lyrics-format",
            type=click.Choice([item.value for item in LyricsFormat]),
            help="Embedded lyrics format (default: text)",
        ),
    ] = LyricsFormat.TEXT.value
    save_playlist: Annotated[
        bool,
        option("--save-playlist/--no-save-playlist", help="Generate M3U for playlists"),
    ] = False
    fail_fast: Annotated[
        bool, option("--fail-fast", is_flag=True, help="Stop after a media error")
    ] = False
    database: Annotated[
        bool, option("--database/--no-database", help="Use the SQLite media index")
    ] = True
    database_path: Annotated[str | None, option("--database-path", type=str)] = None
    save_metadata: Annotated[
        bool,
        option(
            "--save-metadata/--no-save-metadata",
            help="Store resolved media tags in SQLite",
        ),
    ] = False
    no_progress: Annotated[
        bool,
        option("--no-progress", is_flag=True, help="Disable dynamic progress bars"),
    ] = False
    max_visible_tracks: Annotated[
        int, option("--max-visible-tracks", type=click.IntRange(min=1))
    ] = 5
    no_tracebacks: Annotated[
        bool,
        option(
            "--no-tracebacks",
            is_flag=True,
            help="Suppress tracebacks for unexpected errors",
        ),
    ] = False
    auth_mode: Annotated[
        str | None,
        option(
            "--auth-mode",
            type=click.Choice(["wrapper", "cookies", "media-user-token"]),
        ),
    ] = None
    cookies_path: Annotated[str | None, option("--cookies-path", type=str)] = None
    wrapper_url: Annotated[str, option("--wrapper-url", type=str)] = "http://127.0.0.1"
    media_user_token: Annotated[
        str | None,
        option(
            "--media-user-token",
            type=str,
            help="Apple Music token (prefer YAML or environment variables)",
        ),
    ] = None
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


# Library has an implicit URL and always opens its browser. It shares download
# options with rip but does not expose rip's selection-policy flags.
LibraryOptions = make_dataclass(
    "LibraryOptions",
    [
        (item.name, RipOptions.__annotations__[item.name], field(default=item.default))
        for item in fields(RipOptions)
        if item.name
        not in {"urls", "select", "artist_auto_select", "library_auto_select"}
    ]
    + [
        ("select", RipOptions.__annotations__["select"], field(default=False)),
        (
            "library_auto_select",
            Annotated[
                tuple[str, ...],
                option(
                    "--library-auto-select",
                    multiple=True,
                    type=str,
                    help="Library categories, comma-separated or repeated; all = every category",
                ),
            ],
            field(default=()),
        ),
    ],
    namespace={"__module__": __name__},
)


SearchOptions = make_dataclass(
    "SearchOptions",
    [
        (item.name, RipOptions.__annotations__[item.name], field(default=item.default))
        for item in fields(RipOptions)
        if item.name not in {"urls", "select", "artist_auto_select"}
    ]
    + [
        ("term", Annotated[str, argument()], field(default="")),
        (
            "media_type",
            Annotated[
                str | None,
                option(
                    "--type",
                    type=click.Choice(
                        ["song", "music-video", "album", "playlist", "user-playlist"]
                    ),
                ),
            ],
            field(default=None),
        ),
        (
            "feeling_lucky",
            Annotated[
                bool,
                option(
                    "--feeling-lucky",
                    is_flag=True,
                    help="Download the first search result",
                ),
            ],
            field(default=False),
        ),
        (
            "search_api",
            Annotated[
                str,
                option(
                    "--search-api",
                    type=click.Choice(["apple-music", "itunes"]),
                    help="Search Apple Music or the public iTunes catalog",
                ),
            ],
            field(default="apple-music"),
        ),
    ],
    namespace={"__module__": __name__},
)
