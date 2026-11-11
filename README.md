# Gamdl (Glomatico's Apple Music Downloader)

[![PyPI version](https://img.shields.io/pypi/v/gamdl?color=blue)](https://pypi.org/project/gamdl/)
[![Python versions](https://img.shields.io/pypi/pyversions/gamdl)](https://pypi.org/project/gamdl/)
[![License](https://img.shields.io/github/license/glomatico/gamdl)](LICENSE)
[![Downloads](https://img.shields.io/pypi/dm/gamdl)](https://pypi.org/project/gamdl/)

**Gamdl** is a command-line app for downloading Apple Music songs, albums, playlists, music videos, and uploaded videos. It combines an interactive terminal interface with configurable codecs, metadata tagging, and library organization.

**Join our Discord server:** <https://discord.gg/aBjMEZ9tnq>

**Contents**

- [✨ Features](#features)
- [📋 Prerequisites](#prerequisites)
- [📦 Installation](#installation)
- [🚀 First Run](#first-run)
- [🚀 Usage](#usage)
  - [Authentication (`auth`)](#authentication)
- [⚙️ Configuration](#configuration)
- [🔄 Migrating from Gamdl 3.x](#migration)
- [🐍 Python API](#python-api)
- [🛠️ Troubleshooting](#troubleshooting)
- [🤝 Contributing](#contributing)
- [📄 License](#license)

<a name="features"></a>

## ✨ Features

- 🎵 **Music downloads** — Songs, albums, playlists, artists, and your personal Apple Music library.
- 🎬 **Video downloads** — Music videos and uploaded/post videos, with quality and stream selection.
- 🎧 **Multiple codecs** — AAC, ALAC, Dolby Atmos, H.264, H.265, and more, when available for the selected media.
- 📝 **Lyrics and metadata** — Embedded tags, artwork, and optional lyrics and playlist files.
- 🖥️ **Interactive interface** — Full-screen selection menus and live download progress.
- 🗂️ **Library tools** — Customizable paths, an optional SQLite media index, and an offline organizer.

<a name="prerequisites"></a>

## 📋 Prerequisites

- **Python 3.10 or newer.** A Rust toolchain is also needed if your platform must build Gamdl's native extension from source.
- **An Apple Music account.** An active subscription is generally required to access subscription playback streams.
- **An authentication method:** Wrapper, browser cookies, or a Media User Token.
- **A compatible DRM backend** for protected streams. Install the appropriate optional dependency as described below.

Codec and resolution availability varies by track, account, authentication method, and DRM capabilities.

<a name="installation"></a>

## 📦 Installation

Install Gamdl from [PyPI](https://pypi.org/project/gamdl/) with your preferred DRM backend:

```bash
python -m pip install "gamdl[widevine]"
```

For PlayReady instead:

```bash
python -m pip install "gamdl[playready]"
```

The base package can also be installed with `python -m pip install gamdl`, but protected streams require a supported DRM setup or a compatible wrapper. After installation, check the CLI with:

```bash
gamdl --help
```

If a prebuilt wheel is not available for your platform, installing from source requires Rust and the native build dependencies.

<a name="first-run"></a>

## 🚀 First Run

After installing Gamdl, you can start a download right away:

```bash
gamdl rip "https://music.apple.com/us/album/never-gonna-give-you-up-2022-remaster/1624945511?i=1624945512"
```

If this is your first run, Gamdl will guide you through any settings you haven't configured yet:

1. **Authentication:** Choose **Wrapper**, **Cookies**, or **Media User Token**. Follow the on-screen instructions to connect your Apple Music account. See [Authentication](#authentication) for details about each method.
2. **Output directory:** Choose where your downloads will be saved. Gamdl suggests your operating system's **Music** folder; press **Enter** to accept it or enter a different path.

Once setup is complete, Gamdl begins downloading and displays progress in the terminal. It saves your choices in `~/.gamdl/config.yml`, so these prompts won't appear again unless you clear the corresponding settings.

You can change authentication later with `gamdl auth`, or override the download folder for one command with `--output-dir PATH`. For scripts and other non-interactive environments, configure authentication and the output directory beforehand; the setup wizards require an interactive terminal.

<a name="usage"></a>

## 🚀 Usage

Gamdl uses subcommands. Run `gamdl --help` for the command list or `gamdl <command> --help` to see its options.

### Download URLs (`rip`)

Download an Apple Music song, album, playlist, music video, or artist URL:

```bash
gamdl rip "https://music.apple.com/us/album/never-gonna-give-you-up-2022-remaster/1624945511?i=1624945512"
```

Multiple URLs can be downloaded in one invocation:

```bash
gamdl rip "<first Apple Music URL>" "<second Apple Music URL>"
```

Catalog and library sharing links, including Apple Music Classical links, are supported where their corresponding media can be resolved. For artist links, Gamdl opens the artist browser or you can specify categories automatically:

```bash
gamdl rip "<artist URL>" --artist-auto-select main-albums,music-videos
```

Add `--select` to choose album or playlist tracks interactively instead of using the default automatic selection.

### Browse Your Library (`library`)

Browse your saved albums, playlists, songs, and music videos:

```bash
gamdl library
```

For automatic category selection without opening the browser:

```bash
gamdl library --library-auto-select albums,playlists
```

### Search (`search`)

Search the Apple Music catalog and interactively select results:

```bash
gamdl search "Thriller" --type song
```

Switch between the authenticated Apple Music catalog search (default) and
Apple's public iTunes Search API:

```bash
gamdl search "Caravan Palace" --search-api itunes
gamdl search "Caravan Palace" --search-api apple-music
```

Set the backend in YAML to make iTunes the default:

```yaml
search:
  api: itunes
```

Override it for a single command with `--search-api`. iTunes search supports songs,
music videos and albums, but not uploaded videos or playlists. Its public Search API
returns up to 200 results per category; Gamdl displays those in local pages
without relying on undocumented iTunes pagination. Selected items are then
resolved using authoritative Apple Music catalog metadata before download.
Apple documents an approximate iTunes Search API limit of 20 requests/minute;
changing backends doesn't remove throttling limits. Selecting an album expands
its tracks through the existing Apple Music album resolver:

```bash
gamdl search "Thriller" --search-api itunes --type album
```

Apple Music search also offers **Playlists** (editorial and external curators)
and **User playlists** (public, user-shared playlists). The latter are
identified by the catalog's `playlistType: user-shared` field; Apple's search
index may not return any for a particular term/storefront. Selecting a
playlist expands its tracks for download, just like opening a playlist link:

```bash
gamdl search "Michael Jackson" --type playlist
gamdl search "Michael Jackson" --type user-playlist
```

The Apple Music search endpoint does **not** accept `uploaded-videos` as a
search type (HTTP 400). Downloading a supported uploaded-video URL directly
with `gamdl rip` remains available.

Use `--feeling-lucky` with `--type` to select the first result automatically:

```bash
gamdl search "Thriller" --type song --feeling-lucky
```

<a name="authentication"></a>

### Authentication (`auth`)

Authentication is configured automatically during the first run if you haven't selected a method yet. To change your method later, run:

```bash
gamdl auth
```

The wizard validates your selected method and saves it to the YAML configuration.

#### Wrapper

Connect to a running [Wrapper v2](https://github.com/glomatico/wrapper-v2) service. Gamdl can use its authenticated session and compatible playback/decryption features. The default wrapper URL is `http://127.0.0.1`.

#### Cookies

Export your cookies from [Apple Music](https://music.apple.com/) in Netscape `cookies.txt` format while signed in. The file must contain the `media-user-token` cookie. You can use a browser extension such as [Export Cookies for Firefox](https://addons.mozilla.org/addon/export-cookies-txt) or [Get cookies.txt LOCALLY for Chromium](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc).

#### Media User Token

Provide your Apple Music `media-user-token` directly through the wizard. Treat the token and your cookies file as passwords; never post them in issues or logs.

Advanced authentication settings are described under [Configuration](#-configuration).

### Maintain the Media Database (`database`)

The optional SQLite index stores information about downloaded files. You can inspect, scan, and prune its records:

```bash
gamdl database info
gamdl database scan --dry-run
gamdl database scan
gamdl database prune --dry-run
```

`scan` discovers supported local files using their embedded catalog IDs. `prune` removes *database records* for missing files, not media files; it asks for confirmation unless `--yes` is supplied.

### Organize Existing Files (`organize`)

Organize local media using its embedded tags and your configured filename templates:

```bash
gamdl organize --dry-run
gamdl organize
```

The organizer can update database paths and relative M3U playlist references. It avoids overwriting occupied destinations. **Review the dry run first**, especially when changing templates.

When media paths change, `organize` repairs their references inside existing `.m3u8` playlists without changing playlist names or metadata headers. For an already-organized library, use `gamdl organize --playlists-only` (or add `--dry-run` to preview path repairs). Restoring metadata headers in legacy playlists is a separate, manual maintenance task—not an `organize` feature.

### Sync Apple Music Playlists (`sync playlists`)

Reconcile existing local `.m3u8` playlists with their Apple Music catalog IDs, including new, removed, and reordered tracks:

```bash
gamdl sync playlists --dry-run
gamdl sync playlists --dry-run --verbose
gamdl sync playlists
gamdl sync playlists --no-download
gamdl sync playlists --playlist 'Playlists/Apple Music/Oliver Tree_ essenciais.m3u8'
```

Gamdl matches tracks by catalog ID using the local SQLite index and embedded media tags. The default output shows a concise table of only playlists that need attention; use `--verbose` to show all playlists and individual added/removed tracks. After fetching complete playlist listings, synchronization reports the number of **unique missing tracks**, then downloads them with the same per-track progress bars as `rip` and an overall completed/total bar. It respects `download.concurrent_downloads` (parallel tracks) and `download.segment_concurrency` (media segment requests per track), reusing shared tracks across playlists. Missing tracks are downloaded with your configured codec settings unless `--no-download` is used. Removed playlist entries **never delete local media**. Duplicate occurrences and ordered entries are preserved. Failed downloads or incomplete Apple Music responses leave each affected playlist unchanged; other playlists may still succeed. The command does not rename playlist files or alter existing metadata headers. Large removals require `--yes` after inspecting `--dry-run`.

### Interactive Controls

| Key | Action |
| --- | --- |
| Arrow keys | Navigate options |
| Space | Toggle an item in a multi-select menu |
| Ctrl + A | Select all items where supported |
| Enter | Confirm or open the selected item |

<a name="configuration"></a>

## ⚙️ Configuration

Gamdl stores its configuration in a `.gamdl` folder inside your **home directory** (your user profile folder on Windows). For example:

```text
Windows: C:\Users\<username>\.gamdl\config.yml
Linux:   /home/<username>/.gamdl/config.yml
macOS:   /Users/<username>/.gamdl/config.yml
```

Gamdl creates a configuration file automatically on first use when needed. To find its location, create one manually, or inspect the effective settings, use:

```bash
gamdl config path
gamdl config init
gamdl config show
gamdl config validate
```

CLI options override YAML values for the current command. You can specify another file with the top-level `--config PATH` option, or use `--no-config` to ignore your saved settings. Do not run `config init --force` unless you want to replace your existing file.

Option headings below show the **YAML key / CLI flag** where both exist. Options without a YAML key are marked as CLI-only.

### Authentication Settings

#### Authentication Method (`auth.mode` / `--auth-mode`)

Choose `wrapper`, `cookies`, or `media-user-token`. The default is `null`, which opens the setup wizard when authentication is required.

#### Credentials and Wrapper Connection

`auth.cookies_path` / `--cookies-path` specifies the cookies file. `auth.media_user_token` / `--media-user-token` supplies a token directly. `auth.wrapper_url` / `--wrapper-url` sets the wrapper's HTTP address; `auth.wrapper_decrypt_host` / `--wrapper-decrypt-host` and `auth.wrapper_decrypt_port` / `--wrapper-decrypt-port` configure its decryption endpoint (default port `10020`).

Avoid passing secrets on the command line when possible, as they may be recorded in shell history.

#### DRM Backend (`auth.drm_backend` / `--drm-backend`)

Choose `widevine` (default) or `playready`. For a custom provisioned device, use `auth.device_path` / `--device-path`. Widevine can use the bundled default device when applicable; PlayReady requires a compatible device file. Protected media must also have suitable DRM signaling and licenses.

#### Storefront and Language (`auth.storefront` / `--storefront`, `auth.language` / `--language`)

Set the Apple Music storefront with a two-letter country code such as `br`, and the metadata language with a tag such as `pt-BR`. The storefront is normally detected from your account; the default language is `en-US`.

### Song Downloads

#### Codec Priority (`download.song_codec_priority` / `--song-codec-priority`)

Select codecs in preference order. Gamdl tries the preferred formats based on the streams actually available for each track. **Default:** `aac-web`.

```yaml
download:
  song_codec_priority:
    - alac-hires
    - alac-lossless
    - atmos
    - aac-web
```

```bash
gamdl rip "<song URL>" --song-codec-priority alac-hires,alac-lossless,atmos,aac-web
```

Supported song codec identifiers are `aac-web`, `aac-he-web`, `aac`, `aac-he`, `aac-binaural`, `aac-downmix`, `aac-he-binaural`, `aac-he-downmix`, `atmos`, `ac3`, `alac-lossless` (up to 48 kHz), and `alac-hires` (above 48 kHz). ALAC tiers are determined from enhanced-HLS audio-group metadata, rather than the generic codec regex map. To prefer Hi-Res with standard Lossless fallback, list both codecs in that order. The legacy `alac` option is no longer accepted; update existing YAML and CLI preferences.

Gamdl warns after authentication when Apple Music reports account content restrictions, which may prevent some tracks from downloading. It also warns when ALAC is selected without Wrapper, because Apple Music may refuse Widevine/PlayReady licenses for lossless streams. These are informational warnings, not download blockers; interactive ALAC selection displays the warning when that codec is chosen.

#### Exact Codec (`download.song_codec` / `--song-codec`)

Request one specific codec instead of a fallback list. The default is `null`. Use `--ask-song-codec` (CLI-only) to choose a stream interactively; it requires a terminal.

#### Lyrics (`output.no_lyrics_file` / `--no-lyrics-file`)

When metadata tagging is enabled, available song lyrics are embedded in the audio file by default as plain text (`©lyr`), independently of sidecar settings. Gamdl also saves synchronized LRC or plain-text lyric sidecars by default (`no_lyrics_file: false`). Set `output.no_lyrics_file: true` or use `--no-lyrics-file` to disable external lyric files; to omit the embedded tag, add `lyrics` to `output.exclude_tags` or use `--exclude-tags lyrics`. Disabling tagging with `--no-tag` also disables embedded lyrics.

### Music Videos and Uploaded Videos

#### Codec Priority (`download.music_video_codec_priority` / `--music-video-codec-priority`)

Set the order for video codecs: `h264` or `h265`. **Default:** `h264`, then `h265`.

```yaml
download:
  music_video_codec_priority:
    - h265
    - h264
```

```bash
gamdl rip "<music video URL>" --music-video-codec-priority h265,h264
```

#### Video Codec and Resolution (`download.music_video_codec` / `--music-video-codec`, `download.max_height` / `--max-height`)

Select an exact video codec, or keep `null` to use the priority list. The maximum requested height defaults to `1080` pixels; higher resolutions depend on stream availability and your DRM capabilities. Use `--ask-music-video-streams` (CLI-only) to select audio and video streams interactively.

#### Music Video Container (`output.music_video_container` / `--music-video-container`)

Music videos are saved as `.m4v` files by default, with M4V branding. Select `mp4` to save `.mp4` files with `mp42` branding instead; audio and video are still muxed losslessly. This setting does not affect songs or uploaded videos.

```yaml
output:
  music_video_container: mp4
```

```bash
gamdl rip "<music video URL>" --music-video-container mp4
```

#### Uploaded Video Quality (`download.uploaded_video_quality` / `--uploaded-video-quality`)

Choose an uploaded video's asset variant when you need to override automatic quality selection. Use `--ask-uploaded-video-quality` (CLI-only) for an interactive choice.

### Download Behavior

#### Download Concurrency (`download.segment_concurrency` / `--segment-concurrency`, `download.concurrent_downloads` / `--concurrent-downloads`)

`segment_concurrency` controls parallel media-segment HTTP requests for an individual track (**default:** `8`); it does not directly limit Apple Music metadata or playback API requests. `concurrent_downloads` controls how many media items download simultaneously (**default:** `2`). Higher parallelism can indirectly result in overlapping API calls. Gamdl 4.0 uses only `segment_concurrency` and `--segment-concurrency`; the previous names are not supported.

#### Existing Files (`download.overwrite` / `--overwrite`)

Existing files are skipped by default (`download.overwrite: false`). Use the `--overwrite` flag or set `download.overwrite: true` to replace them. `--no-overwrite` overrides a YAML `true` value for one command. Songs use their configured filename directly; music videos can receive numbered suffixes (`Title 2.m4v`) to avoid filename collisions.

#### Error Handling (`download.fail_fast` / `--fail-fast`)

The default is to continue processing remaining items after individual failures (`fail_fast: false`). Set `download.fail_fast: true` or use `--fail-fast` to stop after an error.

For `rip`, `library`, `search`, and `sync playlists`, expected Gamdl errors are reported as short messages without tracebacks. Unexpected exceptions (including DRM license failures and malformed API responses) are collected and printed with **full Python tracebacks after** the final results, once progress bars have closed. Use `--no-tracebacks` (or `ui.no_tracebacks: true` in YAML) to suppress tracebacks while keeping concise failure messages. Tracebacks may contain signed URLs, tokens, or account details; inspect them before sharing logs.

#### Automatic Selection (`download.artist_auto_select` / `--artist-auto-select`, `download.library_auto_select` / `--library-auto-select`)

Choose artist categories such as `main-albums`, `singles-eps`, `top-songs`, and `music-videos`, or library categories such as `albums`, `playlists`, `songs`, and `music-videos`. CLI values are comma-separated; YAML values are lists. Both options default to empty lists, meaning their respective browsers are used.

### Metadata and Artwork

#### Tagging (`output.no_tag` / `--no-tag`, `--tag`)

Metadata tagging is enabled by default (`output.no_tag: false`). Use `--no-tag` to skip writing metadata tags for one command, or `--tag` to enable them even when disabled in YAML.

#### Excluded Tags (`output.exclude_tags` / `--exclude-tags`)

Exclude specific metadata fields, including `cover`, without disabling all tagging. Configure a YAML list or use comma-separated names on the CLI:

```yaml
output:
  no_tag: false
  exclude_tags:
    - comment
    - title_sort
    - cover
```

```bash
gamdl rip "<song URL>" --exclude-tags comment,title_sort,cover
```

#### Artwork (`output.save_cover` / `--save-cover`, `output.cover_format` / `--cover-format`)

Embedded artwork is normally included when tagging is enabled. Saving a separate artwork file is disabled by default. Choose `jpg` (default) or `png` for cover format.

### Files and Folders

#### Output Directory (`output.directory` / `--output-dir`)

The first time you download media, `output.directory` is `null`, so Gamdl asks where to save your music and remembers your choice in `config.yml`. The suggested folder is the **Music directory of your operating system's user profile**, detected through `platformdirs` (including relocated Music folders on Windows and XDG music directories on Linux). Press Enter to accept it, or enter another location.

Use `--output-dir PATH` to bypass the prompt and override the destination for a command, or set `output.directory` directly in YAML. Non-interactive downloads require one of these explicit paths. `output.temp_path` / `--temp-path` optionally selects a working directory for temporary download files.

#### Filename Templates (`output.*_template` / matching CLI options)

Configure album, compilation, no-album, playlist, and filename templates. Each has a corresponding CLI flag, such as `--album-folder-template` or `--single-disc-file-template`.

```yaml
output:
  album_folder_template: "{album_artist}/{album}"
  single_disc_file_template: "{track:02d} {title}"
  multi_disc_file_template: "{disc}-{track:02d} {title}"
  playlist_folder_template: "Playlists/{playlist_artist}"
```

Available fields include `title`, `title_id`, `artist`, `artist_id`, `album`, `album_artist`, `album_id`, `composer`, `composer_id`, `date`, `track`, `track_total`, `disc`, `disc_total`, `media_type`, `playlist_title`, `playlist_artist`, `playlist_id`, and `playlist_track`.

Use `output.truncate` / `--truncate` to limit filename component length when needed.

### Playlists

#### Save Playlists (`output.save_playlist` / `--save-playlist`)

Enable generation of relative-path `.m3u8` files for Apple Music playlists. It is disabled by default. The playlist folder and filename templates live under `output.playlist_folder_template` and `output.playlist_file_template`.

### Database

#### Media Index (`database.disabled` / `--no-database`, `database.path` / `--database-path`)

The SQLite index is enabled by default. It tracks downloaded files with portable paths relative to `output.directory`, their codecs and their most relevant album/playlist source. Disable it with `database.disabled: true` or `--no-database`. The database defaults to `media.sqlite3` in the same `.gamdl` folder as `config.yml`, and `database.path` remains configurable. The first creation of each file is reported as `Created config at …` or `Created database at …`. Custom configuration paths are supported through `--config` or `GAMDL_CONFIG`.

With the index enabled, `rip` checks existing catalog IDs **before** fetching per-track tags, playback streams, lyrics, or decryption information. With overwrite disabled (default), an indexed file that still exists within the output library can be reused directly. Existing cover/lyrics sidecars are also reused without network requests; missing sidecars can still be fetched, and playlist entries are finalized normally. Explicit codec selections, single-codec priorities without a verified matching codec, and `--overwrite` bypass the early skip so codec changes are not silently mistaken for existing matches. The same indexed ID information is used for local track resolution in `sync playlists`.

```yaml
database:
  disabled: false
  save_metadata: false
```

Set `save_metadata: true` (or `--save-metadata`) to also store resolved tags in separate `songs`, `music_videos`, and `uploaded_videos` tables. Metadata writes reuse tags already resolved during downloading and **do not** save artwork or lyrics in SQLite. `gamdl database scan` indexes existing local media, checks local M3U playlists and complete albums to reconstruct source membership, and fills any missing metadata from embedded tags when `save_metadata` is on; it never downloads media or contacts Apple Music.

**Database format change for 4.0:** Older SQLite indexes cannot be opened by this release. Gamdl never overwrites or upgrades them automatically; explicitly move the old database aside or select a fresh `database.path` to initialize the new format. The media files themselves are not touched.

### Interface

#### Progress Display (`ui.no_progress` / `--no-progress`)

Live progress is enabled by default (`ui.no_progress: false`). Set `ui.no_progress: true` or use `--no-progress` to disable it. `ui.max_visible_tracks` / `--max-visible-tracks` controls the number of track rows shown at once (**default:** `5`).

#### Error Tracebacks (`ui.no_tracebacks` / `--no-tracebacks`)

Unexpected errors print full tracebacks at the end by default. Set `ui.no_tracebacks: true` or pass `--no-tracebacks` to hide them. Known, expected conditions remain concise. Review tracebacks before sharing, as exception messages may contain sensitive URLs or account information.

<a name="migration"></a>

## 🔄 Migrating from Gamdl 3.x

Gamdl 4.x introduces several changes to its CLI and configuration:

- **Subcommands:** Use `gamdl rip URL` instead of passing URLs directly to `gamdl`.
- **YAML configuration:** Replace your old INI configuration with the new YAML settings. Run `gamdl config init` to generate a reference configuration.
- **Authentication:** Configure Wrapper, Cookies, or Media User Token with `gamdl auth`; the old `--use-wrapper` flag is replaced by an authentication method.
- **Codec selection:** Interactive stream selection has dedicated flags such as `--ask-song-codec`; codec-priority lists accept comma-separated CLI values or YAML lists.
- **Native processing:** The native muxing/decryption layer replaces the old external downloader and muxer workflow. Review removed 3.x flags rather than copying them directly.
- **Python API:** The modules and classes have been reorganized. Applications embedding Gamdl 3.x should update their imports and integration code.

Keep a backup of your old configuration until you have verified your new settings and output templates.

<a name="python-api"></a>

## 🐍 Python API

Gamdl also provides asynchronous Python APIs for catalog access, media resolution, downloading, and media processing. For example, to fetch a song's metadata using an exported Apple Music cookies file:

```python
import asyncio

from gamdl.api import AppleMusicApi
from gamdl.media import MediaResolver


async def main():
    api = await AppleMusicApi.from_cookies("cookies.txt")
    async with api:
        resolver = MediaResolver(api)
        async for song in resolver.from_song("1228739609"):
            metadata = await song.get_metadata()
            print(metadata["attributes"]["name"])


asyncio.run(main())
```

The API is asynchronous; use context managers to close connections. More advanced applications can use `Downloader`, `Drm`, `Streamer`, `Muxer`, and `PathFormatter` directly.

### HTTP Transports

`gamdl.transport` provides `ApiHttpClient` and `RequestPacer` for private/API requests (authentication, pacing, and retries), alongside `PublicTransport` for public HTTPS resources such as streaming manifests, initialization segments, and artwork. The public transport uses isolated requests at every redirect hop, so credentials and cookies from a caller-supplied HTTPX client are not forwarded. It rejects insecure redirects and preserves the lifetime of borrowed clients. HLS parsing stays in `media.stream.StreamTransport`, which uses `PublicTransport` for network requests. Existing `gamdl.api` and `gamdl.api.http` imports of `ApiHttpClient` / `RequestPacer` remain supported.

<a name="troubleshooting"></a>

## 🛠️ Troubleshooting

**Authentication fails:** Check that your session is valid, your cookies include `media-user-token`, or your wrapper is reachable. Retry `gamdl auth`.

**A codec or stream is unavailable:** Not every track offers every codec. Try `--ask-song-codec`, `--ask-music-video-streams`, or a more conservative codec priority such as `aac-web`.

**DRM initialization fails:** Verify that the relevant extra (`widevine` or `playready`) is installed, that a required device file is available, and that the stream supports the selected backend.

**A download is skipped:** The default is `download.overwrite: false`. Use `--overwrite` if you intentionally want to replace the existing file.

**The library has unexpected paths:** Check your filename templates and use `gamdl organize --dry-run` before applying changes.

For additional support, use [GitHub Issues](https://github.com/glomatico/gamdl/issues) or the [Discord server](https://discord.gg/aBjMEZ9tnq). When sharing error tracebacks, remove cookies, tokens, and signed URLs.

<a name="contributing"></a>

## 🤝 Contributing

Bug reports, feature requests, and pull requests are welcome. Please check existing [issues](https://github.com/glomatico/gamdl/issues) before opening a new one.

For source development, install [uv](https://docs.astral.sh/uv/), Python, and Rust. Then run:

```bash
uv sync
uv run python -m unittest discover -s tests
cargo test --manifest-path native/Cargo.toml
```

Some media-related tests also require FFmpeg and FFprobe.

<a name="license"></a>

## 📄 License

Gamdl is released under the [MIT License](LICENSE).
