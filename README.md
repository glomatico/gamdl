# Gamdl (Glomatico's Apple Music Downloader)

[![PyPI version](https://img.shields.io/pypi/v/gamdl?color=blue)](https://pypi.org/project/gamdl/)
[![Python versions](https://img.shields.io/pypi/pyversions/gamdl)](https://pypi.org/project/gamdl/)
[![License](https://img.shields.io/github/license/glomatico/gamdl)](https://github.com/glomatico/gamdl/blob/main/LICENSE)
[![Downloads](https://img.shields.io/pypi/dm/gamdl)](https://pypi.org/project/gamdl/)

A command-line app for downloading Apple Music songs, music videos and post videos.

**Join our Discord Server:** <https://discord.gg/aBjMEZ9tnq>

## ✨ Features

- 🎵 **High-Quality Songs** - Download songs in AAC 256kbps and other codecs
- 🎬 **High-Quality Music Videos** - Download music videos in resolutions up to 4K
- 📝 **Synced Lyrics** - Download synced lyrics in LRC, SRT, or TTML formats
- 🏷️ **Rich Metadata** - Automatic tagging with comprehensive metadata
- 🎤 **Artist Support** - Download all albums or music videos from an artist
- ⚙️ **Highly Customizable** - Extensive configuration options for advanced users

## 📋 Prerequisites

### Required

- **Python 3.10 or higher**
- **Apple Music Cookies** - Export your browser cookies in Netscape format while logged in with an active subscription at the Apple Music website:
  - **Firefox**: [Export Cookies](https://addons.mozilla.org/addon/export-cookies-txt)
  - **Chromium**: [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
- **FFmpeg** - Must be in your system PATH
  - **Windows**: [AnimMouse's FFmpeg Builds](https://github.com/AnimMouse/ffmpeg-stable-autobuild/releases)
  - **Linux**: [John Van Sickle's FFmpeg Builds](https://johnvansickle.com/ffmpeg/)

### Optional

Add these tools to your system PATH for additional features:

- **[mp4decrypt](https://www.bento4.com/downloads/)** - Required for `mp4box` remux mode, music videos, and experimental codecs
- **[MP4Box](https://gpac.io/downloads/gpac-nightly-builds/)** - Required for `mp4box` remux mode
- **[N_m3u8DL-RE](https://github.com/nilaoda/N_m3u8DL-RE/releases/latest)** - Required for `nm3u8dlre` download mode, which is faster than the default downloader
- **[Wrapper](#️-wrapper)** - For downloading songs in ALAC and other experimental codecs without API limitations

## 📦 Installation

**Install Gamdl via pip:**

```bash
pip install gamdl
```

**Setup cookies:**

1. Place your cookies file in the working directory as `cookies.txt`, or
2. Specify the path using `--cookies-path` or in the config file

## 🚀 Usage

```bash
gamdl [OPTIONS] URLS...
```

### Supported URL Types

- Songs
- Albums (Public/Library)
- Playlists (Public/Library)
- Music Videos
- Artists
- Post Videos

### Examples

**Download a song:**

```bash
gamdl "https://music.apple.com/us/album/never-gonna-give-you-up-2022-remaster/1624945511?i=1624945512"
```

**Download an album:**

```bash
gamdl "https://music.apple.com/us/album/whenever-you-need-somebody-2022-remaster/1624945511"
```

**Download from an artist:**

```bash
gamdl "https://music.apple.com/us/artist/rick-astley/669771"
```

**Interactive Prompt Controls:**

| Key            | Action            |
| -------------- | ----------------- |
| **Arrow keys** | Move selection    |
| **Space**      | Toggle selection  |
| **Ctrl + A**   | Select all        |
| **Enter**      | Confirm selection |

## ⚙️ Configuration

Configure Gamdl using command-line arguments or a config file.

**Config file location:**

- Linux: `~/.gamdl/config.ini`
- Windows: `%USERPROFILE%\.gamdl\config.ini`

The file is created automatically on first run. Command-line arguments override config values.

### Configuration Options

| Option                          | Description                     | Default                                        |
| ------------------------------- | ------------------------------- | ---------------------------------------------- |
| **General Options**             |                                 |                                                |
| `--read-urls-as-txt`, `-r`      | Read URLs from text files       | `false`                                        |
| `--config-path`                 | Config file path                | `<home>/.gamdl/config.ini`                     |
| `--log-level`                   | Logging level                   | `INFO`                                         |
| `--log-file`                    | Log file path                   | -                                              |
| `--no-exceptions`               | Don't print exceptions          | `false`                                        |
| `--no-config-file`, `-n`        | Don't use a config file         | `false`                                        |
| **Apple Music Options**         |                                 |                                                |
| `--cookies-path`, `-c`          | Cookies file path               | `./cookies.txt`                                |
| `--wrapper-account-url`         | Wrapper account URL             | `http://127.0.0.1:30020`                       |
| `--language`, `-l`              | Metadata language               | `en-US`                                        |
| **Output Options**              |                                 |                                                |
| `--output-path`, `-o`           | Output directory path           | `./Apple Music`                                |
| `--temp-path`                   | Temporary directory path        | `.`                                            |
| `--wvd-path`                    | .wvd file path                  | -                                              |
| `--overwrite`                   | Overwrite existing files        | `false`                                        |
| `--save-cover`, `-s`            | Save cover as separate file     | `false`                                        |
| `--save-playlist`               | Save M3U8 playlist file         | `false`                                        |
| **Download Options**            |                                 |                                                |
| `--nm3u8dlre-path`              | N_m3u8DL-RE executable path     | `N_m3u8DL-RE`                                  |
| `--mp4decrypt-path`             | mp4decrypt executable path      | `mp4decrypt`                                   |
| `--ffmpeg-path`                 | FFmpeg executable path          | `ffmpeg`                                       |
| `--mp4box-path`                 | MP4Box executable path          | `MP4Box`                                       |
| `--use-wrapper`                 | Use wrapper                     | `false`                                        |
| `--wrapper-decrypt-ip`          | Wrapper decryption server IP    | `127.0.0.1:10020`                              |
| `--download-mode`               | Download mode                   | `ytdlp`                                        |
| `--remux-mode`                  | Remux mode                      | `ffmpeg`                                       |
| `--cover-format`                | Cover format                    | `jpg`                                          |
| **Template Options**            |                                 |                                                |
| `--album-folder-template`       | Album folder template           | `{album_artist}/{album}`                       |
| `--compilation-folder-template` | Compilation folder template     | `Compilations/{album}`                         |
| `--no-album-folder-template`    | No album folder template        | `{artist}/Unknown Album`                       |
| `--single-disc-file-template`   | Single disc file template       | `{track:02d} {title}`                          |
| `--multi-disc-file-template`    | Multi disc file template        | `{disc}-{track:02d} {title}`                   |
| `--no-album-file-template`      | No album file template          | `{title}`                                      |
| `--playlist-file-template`      | Playlist file template          | `Playlists/{playlist_artist}/{playlist_title}` |
| `--date-tag-template`           | Date tag template               | `%Y-%m-%dT%H:%M:%SZ`                           |
| `--exclude-tags`                | Comma-separated tags to exclude | -                                              |
| `--cover-size`                  | Cover size in pixels            | `1200`                                         |
| `--truncate`                    | Max filename length             | -                                              |
| **Song Options**                |                                 |                                                |
| `--song-codec`                  | Song codec                      | `aac-legacy`                                   |
| `--synced-lyrics-format`        | Synced lyrics format            | `lrc`                                          |
| `--no-synced-lyrics`            | Don't download synced lyrics    | `false`                                        |
| `--synced-lyrics-only`          | Download only synced lyrics     | `false`                                        |
| `--use-album-date`              | Use album release date for songs | `false`                                        |
| `--fetch-extra-tags`            | Fetch extra tags from preview (normalization and smooth playback) | `false`                                        |
| **Music Video Options**         |                                 |                                                |
| `--music-video-codec-priority`  | Comma-separated codec priority  | `h264,h265`                                    |
| `--music-video-remux-format`    | Music video remux format        | `m4v`                                          |
| `--music-video-resolution`      | Max music video resolution      | `1080p`                                        |
| **Post Video Options**          |                                 |                                                |
| `--uploaded-video-quality`      | Post video quality              | `best`                                         |

### Template Variables

**Tags for templates and exclude-tags:**

- `album`, `album_artist`, `album_id`
- `artist`, `artist_id`
- `composer`, `composer_id`
- `date` (supports strftime format: `{date:%Y}`)
- `disc`, `disc_total`
- `media_type`
- `playlist_artist`, `playlist_id`, `playlist_title`, `playlist_track`
- `title`, `title_id`
- `track`, `track_total`

**Tags for exclude-tags only:**

- `album_sort`, `artist_sort`, `composer_sort`, `title_sort`
- `comment`, `compilation`, `copyright`, `cover`, `gapless`, `genre`, `genre_id`, `lyrics`, `rating`, `storefront`, `xid`
- `all` (special: skip all tagging)

### Logging Level

- `DEBUG`, `INFO`, `WARNING`, `ERROR`

### Download Mode

- `ytdlp`, `nm3u8dlre`

### Remux Mode

- `ffmpeg`
- `mp4box` - Preserve the original closed caption track in music videos and some other minor metadata

### Cover Format

- `jpg`
- `png`
- `raw` - Raw format as provided by the artist (requires `save_cover` to be enabled as it doesn't embed covers into files)

### Metadata Language

Use ISO 639-1 language codes (e.g., `en-US`, `es-ES`, `ja-JP`, `pt-BR`). Don't always work for music videos.

### Song Codecs

**Stable:**

- `aac-legacy` - AAC 256kbps 44.1kHz
- `aac-he-legacy` - AAC-HE 64kbps 44.1kHz

**Experimental** (may not work due to API limitations):

- `aac` - AAC 256kbps up to 48kHz
- `aac-he` - AAC-HE 64kbps up to 48kHz
- `aac-binaural` - AAC 256kbps binaural
- `aac-downmix` - AAC 256kbps downmix
- `aac-he-binaural` - AAC-HE 64kbps binaural
- `aac-he-downmix` - AAC-HE 64kbps downmix
- `atmos` - Dolby Atmos 768kbps
- `ac3` - AC3 640kbps
- `alac` - ALAC up to 24-bit/192kHz (unsupported)
- `ask` - Interactive experimental codec selection

### Synced Lyrics Format

- `lrc`
- `srt` - SubRip subtitle format (more accurate timing)
- `ttml` - Native Apple Music format (not compatible with most media players)

### Music Video Codecs

- `h264`
- `h265`
- `ask` - Interactive codec selection

### Music Video Resolutions

- H.264: `240p`, `360p`, `480p`, `540p`, `720p`, `1080p`
- H.265 only: `1440p`, `2160p`

### Music Video Remux Formats

- `m4v`, `mp4`

### Post Video Quality

- `best` - Up to 1080p with AAC 256kbps
- `ask` - Interactive quality selection

## ⚙️ Wrapper

Use the [wrapper](https://github.com/WorldObservationLog/wrapper) to download songs in ALAC and other experimental codecs without API limitations. Cookies, FFmpeg, MP4Box, or mp4decrypt are not required when using the wrapper.

### Setup Instructions

1. **Start the wrapper server** - Run the wrapper server
2. **Enable wrapper in Gamdl** - Use `--use-wrapper` flag or set `use_wrapper = true` in config
3. **Run Gamdl** - Download as usual with the wrapper enabled

## 🐍 Embedding

Use Gamdl as a library in your Python projects:

```python
import asyncio

from gamdl.api import AppleMusicApi, ItunesApi
from gamdl.downloader import (
    AppleMusicBaseDownloader,
    AppleMusicDownloader,
    AppleMusicMusicVideoDownloader,
    AppleMusicSongDownloader,
    AppleMusicUploadedVideoDownloader,
)
from gamdl.interface import (
    AppleMusicInterface,
    AppleMusicMusicVideoInterface,
    AppleMusicSongInterface,
    AppleMusicUploadedVideoInterface,
)

async def main():
    # Create AppleMusicApi instance (from cookies or wrapper)
    apple_music_api = await AppleMusicApi.create_from_netscape_cookies(
        cookies_path="cookies.txt",
    )
    itunes_api = ItunesApi(
        apple_music_api.storefront,
        apple_music_api.language,
    )

    # Check subscription
    assert apple_music_api.active_subscription

    # Set up interfaces
    interface = AppleMusicInterface(apple_music_api, itunes_api)
    song_interface = AppleMusicSongInterface(interface)
    music_video_interface = AppleMusicMusicVideoInterface(interface)
    uploaded_video_interface = AppleMusicUploadedVideoInterface(interface)

    # Set up base downloader and specialized downloaders
    base_downloader = AppleMusicBaseDownloader()
    song_downloader = AppleMusicSongDownloader(
        base_downloader=base_downloader,
        interface=song_interface,
    )
    music_video_downloader = AppleMusicMusicVideoDownloader(
        base_downloader=base_downloader,
        interface=music_video_interface,
    )
    uploaded_video_downloader = AppleMusicUploadedVideoDownloader(
        base_downloader=base_downloader,
        interface=uploaded_video_interface,
    )

    # Main downloader
    downloader = AppleMusicDownloader(
        interface=interface,
        base_downloader=base_downloader,
        song_downloader=song_downloader,
        music_video_downloader=music_video_downloader,
        uploaded_video_downloader=uploaded_video_downloader,
    )

    # Download a song
    url = "https://music.apple.com/us/album/never-gonna-give-you-up-2022-remaster/1624945511?i=1624945512"
    url_info = downloader.get_url_info(url)
    if url_info:
        download_queue = await downloader.get_download_queue(url_info)
        if download_queue:
            for download_item in download_queue:
                await downloader.download(download_item)


if __name__ == "__main__":
    asyncio.run(main())
```

## 📄 License

MIT License - see [LICENSE](LICENSE) file for details

## 🤝 Contributing

Currently, I'm not interested in reviewing pull requests that change or add features. Only critical bug fixes will be considered. However, feel free to open issues for bugs or feature requests.
