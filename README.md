# Glomatico’s Apple Music Downloader

A Python CLI app for downloading Apple Music songs, music videos and post videos.

**Join our Discord Server:** https://discord.gg/aBjMEZ9tnq

## Features

- **High-Quality Songs**: Download songs in AAC 256kbps and other codecs.
- **High-Quality Music Videos**: Download music videos in resolutions up to 4K.
- **Synced Lyrics**: Download synced lyrics in LRC, SRT, or TTML formats.
- **Artist Support**: Download all albums or music videos from an artist using their link.
- **Highly Customizable**: Extensive configuration options for advanced users.

## Prerequisites

- **Python 3.10 or higher** installed on your system.
- The **cookies file** of your Apple Music browser session in Netscape format (requires an active subscription).
  - **Firefox**: Use the [Export Cookies](https://addons.mozilla.org/addon/export-cookies-txt) extension.
  - **Chromium-based Browsers**: Use the [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) extension.
- **FFmpeg** on your system PATH.
  - **Windows**: Download from [AnimMouse’s FFmpeg Builds](https://github.com/AnimMouse/ffmpeg-stable-autobuild/releases).
  - **Linux**: Download from [John Van Sickle’s FFmpeg Builds](https://johnvansickle.com/ffmpeg/).

### Optional dependencies

The following tools are optional but required for specific features. Add them to your system’s PATH or specify their paths using command-line arguments or the config file.

- [mp4decrypt](https://www.bento4.com/downloads/): Required for `mp4box` remux mode, music video downloads, and experimental song codecs.
- [MP4Box](https://gpac.io/downloads/gpac-nightly-builds/): Required for `mp4box` remux mode.
- [N_m3u8DL-RE](https://github.com/nilaoda/N_m3u8DL-RE/releases/latest): Required for `nm3u8dlre` download mode.

## Installation

1. Install the package `gamdl` using pip
   ```bash
   pip install gamdl
   ```
2. Set up the cookies file.
   - Move the cookies file to the directory where you’ll run Gamdl and rename it to `cookies.txt`.
   - Alternatively, specify the path to the cookies file using command-line arguments or the config file.

## Usage

Run Gamdl with the following command:

```bash
gamdl [OPTIONS] URLS...
```

### Supported URL types

- Song
- Public/Library Album
- Public/Library Playlist
- Music video
- Artist
- Post video

### Examples

- Download a Song:
  ```bash
  gamdl "https://music.apple.com/us/album/never-gonna-give-you-up-2022-remaster/1624945511?i=1624945512"
  ```
- Download an Album:
  ```bash
  gamdl "https://music.apple.com/us/album/whenever-you-need-somebody-2022-remaster/1624945511"
  ```
- Download from an Artist:
  ```bash
  gamdl "https://music.apple.com/us/artist/rick-astley/669771"
  ```

### Interactive prompt controls

- **Arrow keys**: Move selection
- **Space**: Toggle selection
- **Ctrl + A**: Select all
- **Enter**: Confirm selection

## Configuration

Gamdl can be configured by using the command-line arguments or the config file.

The config file is created automatically when you run Gamdl for the first time at `~/.gamdl/config.json` on Linux and `%USERPROFILE%\.gamdl\config.json` on Windows.

Config file values can be overridden using command-line arguments.
| Command-line argument / Config file key | Description | Default value |
| --------------------------------------------------------------- | ---------------------------------------------------------------------------- | ---------------------------- |
| `--disable-music-video-skip` / `disable_music_video_skip` | Don't skip downloading music videos in albums/playlists. | `false` |
| `--save-cover`, `-s` / `save_cover` | Save cover as a separate file. | `false` |
| `--overwrite` / `overwrite` | Overwrite existing files. | `false` |
| `--read-urls-as-txt`, `-r` / - | Interpret URLs as paths to text files containing URLs separated by newlines. | `false` |
| `--save-playlist` / `save_playlist` | Save a M3U8 playlist file when downloading a playlist. | `false` |
| `--synced-lyrics-only` / `synced_lyrics_only` | Download only the synced lyrics. | `false` |
| `--no-synced-lyrics` / `no_synced_lyrics` | Don't download the synced lyrics. | `false` |
| `--config-path` / - | Path to config file. | `<home>/.gamdl/config.json` |
| `--log-level` / `log_level` | Log level. | `INFO` |
| `--no-exceptions` / `no_exceptions` | Don't print exceptions. | `false` |
| `--cookies-path`, `-c` / `cookies_path` | Path to .txt cookies file. | `./cookies.txt` |
| `--language`, `-l` / `language` | Metadata language as an ISO-2A language code (don't always work for videos). | `en-US` |
| `--output-path`, `-o` / `output_path` | Path to output directory. | `./Apple Music` |
| `--temp-path` / `temp_path` | Path to temporary directory. | `./temp` |
| `--wvd-path` / `wvd_path` | Path to .wvd file. | `null` |
| `--nm3u8dlre-path` / `nm3u8dlre_path` | Path to N_m3u8DL-RE binary. | `N_m3u8DL-RE` |
| `--mp4decrypt-path` / `mp4decrypt_path` | Path to mp4decrypt binary. | `mp4decrypt` |
| `--ffmpeg-path` / `ffmpeg_path` | Path to FFmpeg binary. | `ffmpeg` |
| `--mp4box-path` / `mp4box_path` | Path to MP4Box binary. | `MP4Box` |
| `--download-mode` / `download_mode` | Download mode. | `ytdlp` |
| `--remux-mode` / `remux_mode` | Remux mode. | `ffmpeg` |
| `--cover-format` / `cover_format` | Cover format. | `jpg` |
| `--template-folder-album` / `template_folder_album` | Template folder for tracks that are part of an album. | `{album_artist}/{album}` |
| `--template-folder-compilation` / `template_folder_compilation` | Template folder for tracks that are part of a compilation album. | `Compilations/{album}` |
| `--template-file-single-disc` / `template_file_single_disc` | Template file for the tracks that are part of a single-disc album. | `{track:02d} {title}` |
| `--template-file-multi-disc` / `template_file_multi_disc` | Template file for the tracks that are part of a multi-disc album. | `{disc}-{track:02d} {title}` |
| `--template-folder-no-album` / `template_folder_no_album` | Template folder for the tracks that are not part of an album. | `{artist}/Unknown Album` |
| `--template-file-no-album` / `template_file_no_album` | Template file for the tracks that are not part of an album. | `{title}` |
| `--template-file-playlist` / `template_file_playlist` | Template file for the M3U8 playlist. | `Playlists/{playlist_title}` |
| `--template-date` / `template_date` | Date tag template. | `%Y-%m-%dT%H:%M:%SZ` |
| `--exclude-tags` / `exclude_tags` | Comma-separated tags to exclude. | `null` |
| `--cover-size` / `cover_size` | Cover size. | `1200` |
| `--truncate` / `truncate` | Maximum length of the file/folder names. | `null` |
| `--codec-song` / `codec_song` | Song codec. | `aac-legacy` |
| `--synced-lyrics-format` / `synced_lyrics_format` | Synced lyrics format. | `lrc` |
| `--codec-music-video` / `codec_music_video` | Music video codec. | `h264` |
| `--remux-format-music-video` / `remux_format_music_video` | Music video remux format. | `m4v` |
| `--quality-post` / `quality_post` | Post video quality. | `best` |
| `--no-config-file`, `-n` / - | Do not use a config file. | `false` |

### Tags variables

The following variables can be used in the template folders/files and/or in the `exclude_tags` list:

- `album`
- `album_artist`
- `album_id`
- `album_sort`
- `artist`
- `artist_id`
- `artist_sort`
- `comment`
- `compilation`
- `composer`
- `composer_id`
- `composer_sort`
- `copyright`
- `cover`
- `date`
- `disc`
- `disc_total`
- `gapless`
- `genre`
- `genre_id`
- `lyrics`
- `media_type`
- `playlist_artist`
- `playlist_id`
- `playlist_title`
- `playlist_track`
- `rating`
- `storefront`
- `title`
- `title_id`
- `title_sort`
- `track`
- `track_total`
- `xid`

### Remux Modes

- `ffmpeg`: Default remuxing mode.
- `mp4box`: Alternative remuxing mode (doesn’t convert closed captions in music videos).

### Download modes

- `ytdlp`: Default download mode.
- `nm3u8dlre`: Faster than `ytdlp`.

### Song Codecs

- Supported Codecs:
  - `aac-legacy`: AAC 256kbps 44.1kHz.
  - `aac-he-legacy`: AAC-HE 64kbps 44.1kHz.
- Experimental Codecs (not guaranteed to work due to API limitations):
  - `aac`: AAC 256kbps up to 48kHz.
  - `aac-he`: AAC-HE 64kbps up to 48kHz.
  - `aac-binaural`: AAC 256kbps binaural.
  - `aac-downmix`: AAC 256kbps downmix.
  - `aac-he-binaural`: AAC-HE 64kbps binaural.
  - `aac-he-downmix`: AAC-HE 64kbps downmix.
  - `atmos`: Dolby Atmos 768kbps.
  - `ac3`: AC3 640kbps.
  - `alac`: ALAC up to 24-bit/192 kHz.
  - `ask`: Prompt to choose available audio codec.

### Music Videos Codecs

- `h264`: Up to 1080p with AAC 256kbps.
- `h265`: Up to 2160p with AAC 256kpbs.
- `ask`: Prompt to choose available video and audio codecs.

### Music Videos Remux Formats

- `m4v`: Default remux format.
- `mp4`

### Post videos/extra videos qualities

- `best`: Up to 1080p with AAC 256kbps.
- `ask`: Prompt to choose available video quality.

### Synced lyrics formats

- `lrc`: Lightweight and widely supported.
- `srt`: SubRip format (has more accurate timestamps).
- `ttml`: Native Apple Music format (unsupported by most media players).

### Cover formats

- `jpg`: Default format.
- `png`: Lossless format.
- `raw`: Raw cover without processing (requires `save_cover` to save separately).
