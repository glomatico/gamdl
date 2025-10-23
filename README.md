# Glomatico's Apple Music Downloader

A command-line app for downloading Apple Music songs, music videos and post videos.

**Join our Discord Server:** <https://discord.gg/aBjMEZ9tnq>

## Features

- **High-Quality Songs**: Download songs in AAC 256kbps and other codecs.
- **High-Quality Music Videos**: Download music videos in resolutions up to 4K.
- **Synced Lyrics**: Download synced lyrics in LRC, SRT, or TTML formats.
- **Artist Support**: Download all albums or music videos from an artist using their link.
- **Highly Customizable**: Extensive configuration options for advanced users.

## Prerequisites

- **Python 3.10 or higher** installed on your system.
- The **cookies file** of your Apple Music browser session in Netscape format. Use one of the following extensions at the Apple Music homepage while logged in and with an active subscription to export the cookies:
  - **Firefox**: [Export Cookies](https://addons.mozilla.org/addon/export-cookies-txt).
  - **Chromium-based Browsers**: [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc).
- **FFmpeg** on your system PATH. Use one of the recommended builds:
  - **Windows**: [AnimMouse's FFmpeg Builds](https://github.com/AnimMouse/ffmpeg-stable-autobuild/releases).
  - **Linux**: [John Van Sickle's FFmpeg Builds](https://johnvansickle.com/ffmpeg/).

### Optional dependencies

The following tools are optional but required for specific features. Add them to your system's PATH or specify their paths using command-line arguments or the config file.

- [mp4decrypt](https://www.bento4.com/downloads/): Required for `mp4box` remux mode, music video downloads, and experimental song codecs.
- [MP4Box](https://gpac.io/downloads/gpac-nightly-builds/): Required for `mp4box` remux mode.
- [N_m3u8DL-RE](https://github.com/nilaoda/N_m3u8DL-RE/releases/latest): Required for `nm3u8dlre` download mode.

## Installation

1. Install the package `gamdl` using pip

   ```bash
   pip install gamdl
   ```

2. Set up the cookies file.
   - Move the cookies file to the directory where you'll run Gamdl and rename it to `cookies.txt`.
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

The config file is created automatically when you run Gamdl for the first time at `~/.gamdl/config.ini` on Linux and `%USERPROFILE%\.gamdl\config.ini` on Windows.

Config file values can be overridden using command-line arguments.

| Command-line argument / Config file key                         | Description                                                                  | Default value                                  |
| --------------------------------------------------------------- | ---------------------------------------------------------------------------- | ---------------------------------------------- |
| `--read-urls-as-txt`, `-r` / -                                  | Interpret URLs as paths to text files containing URLs separated by newlines  | `false`                                        |
| `--config-path` / -                                             | Path to config file.                                                         | `<home>/.gamdl/config.ini`                     |
| `--log-level` / `log_level`                                     | Log level.                                                                   | `INFO`                                         |
| `--log-file` / `log_file`                                       | Path to log file.                                                            | `null`                                         |
| `--no-exceptions` / `no_exceptions`                             | Don't print exceptions.                                                      | `false`                                        |
| `--cookies-path`, `-c` / `cookies_path`                         | Path to .txt cookies file.                                                   | `./cookies.txt`                                |
| `--language`, `-l` / `language`                                 | Metadata language as an ISO-2A language code (don't always work for videos). | `en-US`                                        |
| `--output-path`, `-o` / `output_path`                           | Path to output directory.                                                    | `./Apple Music`                                |
| `--temp-path` / `temp_path`                                     | Path to temporary directory.                                                 | `.`                                            |
| `--wvd-path` / `wvd_path`                                       | Path to .wvd file.                                                           | `null`                                         |
| `--overwrite` / `overwrite`                                     | Overwrite existing files.                                                    | `false`                                        |
| `--save-cover`, `-s` / `save_cover`                             | Save cover as a separate file.                                               | `false`                                        |
| `--save-playlist` / `save_playlist`                             | Save a M3U8 playlist file when downloading a playlist.                       | `false`                                        |
| `--nm3u8dlre-path` / `nm3u8dlre_path`                           | Path to N_m3u8DL-RE binary.                                                  | `N_m3u8DL-RE`                                  |
| `--mp4decrypt-path` / `mp4decrypt_path`                         | Path to mp4decrypt binary.                                                   | `mp4decrypt`                                   |
| `--ffmpeg-path` / `ffmpeg_path`                                 | Path to FFmpeg binary.                                                       | `ffmpeg`                                       |
| `--mp4box-path` / `mp4box_path`                                 | Path to MP4Box binary.                                                       | `MP4Box`                                       |
| `--download-mode` / `download_mode`                             | Download mode.                                                               | `ytdlp`                                        |
| `--remux-mode` / `remux_mode`                                   | Remux mode.                                                                  | `ffmpeg`                                       |
| `--cover-format` / `cover_format`                               | Cover format.                                                                | `jpg`                                          |
| `--album-folder-template` / `album_folder_template`             | Template folder for tracks that are part of an album.                        | `{album_artist}/{album}`                       |
| `--compilation-folder-template` / `compilation_folder_template` | Template folder for tracks that are part of a compilation album.             | `Compilations/{album}`                         |
| `--single-disc-folder-template` / `single_disc_folder_template` | Template file for the tracks that are part of a single-disc album.           | `{track:02d} {title}`                          |
| `--multi-disc-folder-template` / `multi_disc_folder_template`   | Template file for the tracks that are part of a multi-disc album.            | `{disc}-{track:02d} {title}`                   |
| `--no-album-folder-template` / `no_album_folder_template`       | Template folder for the tracks that are not part of an album.                | `{artist}/Unknown Album`                       |
| `--no-album-file-template` / `no_album_file_template`           | Template file for the tracks that are not part of an album.                  | `{title}`                                      |
| `--playlist-file-template` / `playlist_file_template`           | Template file for the M3U8 playlist.                                         | `Playlists/{playlist_artist}/{playlist_title}` |
| `--date-tag-template` / `date_tag_template`                     | Date tag template.                                                           | `%Y-%m-%dT%H:%M:%SZ`                           |
| `--exclude-tags` / `exclude_tags`                               | Comma-separated tags to exclude.                                             | `null`                                         |
| `--cover-size` / `cover_size`                                   | Cover size.                                                                  | `1200`                                         |
| `--truncate` / `truncate`                                       | Maximum length of the file/folder names.                                     | `null`                                         |
| `--codec-song` / `codec_song`                                   | Song codec.                                                                  | `aac-legacy`                                   |
| `--synced-lyrics-format` / `synced_lyrics_format`               | Synced lyrics format.                                                        | `lrc`                                          |
| `--no-synced-lyrics` / `no_synced_lyrics`                       | Don't download the synced lyrics.                                            | `false`                                        |
| `--synced-lyrics-only` / `synced_lyrics_only`                   | Download only the synced lyrics.                                             | `false`                                        |
| `--music-video-codec-priority` / `music_video_codec_priority`   | Comma-separated music video codec priority.                                  | `h265,h264`                                    |
| `--music-video-remux-format` / `music_video_remux_format`       | Music video remux format.                                                    | `m4v`                                          |
| `--music-video-resolution` / `music_video_resolution`           | Target video resolution for music videos.                                    | `1080p`                                        |
| `--uploaded-video-quality` / `uploaded_video_quality`           | Upload videos quality.                                                       | `best`                                         |
| `--no-config-file`, `-n` / -                                    | Do not use a config file.                                                    | `false`                                        |

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
- `date`: Supports strftime formats. For example, `{date:%Y}` will be replaced with the year of the release date.
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
- `all`: Skip tagging.

### Remux Modes

- `ffmpeg`: Default remuxing mode.
- `mp4box`: Alternative remuxing mode (doesn't convert closed captions in music videos).

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
  - `alac`: ALAC up to 24-bit/192 kHz (no reports of successful downloads have been made).
  - `ask`: Prompt to choose available audio codec.

### Music Videos Codecs

- `h264`
- `h265`
- `ask`: Prompt to choose available video and audio codecs.

### Music Videos Remux Formats

- `m4v`: Default remux format.
- `mp4`

### Music Videos Maximum Resolutions

- H.264 Resolutions:
  - `240p`
  - `360p`
  - `480p`
  - `540p`
  - `720p`
  - `1080p`
- H.265-only Resolutions:
  - `1440p`
  - `2160p`

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

## Embedding

Gamdl can be used as an async library in Python scripts. Here's a basic example of downloading a song by its URL:

```python
import asyncio

from gamdl.api import AppleMusicApi
from gamdl.downloader import (
    AppleMusicBaseDownloader,
    AppleMusicDownloader,
    AppleMusicMusicVideoDownloader,
    AppleMusicSongDownloader,
    AppleMusicUploadedVideoDownloader,
)


async def main():
    # Initialize the Apple Music API
    api = AppleMusicApi.from_netscape_cookies(cookies_path="cookies.txt")
    await api.setup()

    # Initialize the base downloader
    base_downloader = AppleMusicBaseDownloader(apple_music_api=api)
    base_downloader.setup()

    # Initialize the song downloader
    song_downloader = AppleMusicSongDownloader(base_downloader)
    song_downloader.setup()

    # Initialize the music video downloader
    music_video_downloader = AppleMusicMusicVideoDownloader(base_downloader)
    music_video_downloader.setup()

    # Initialize the uploaded video downloader
    uploaded_video_downloader = AppleMusicUploadedVideoDownloader(base_downloader)
    uploaded_video_downloader.setup()

    # Initialize the main downloader
    downloader = AppleMusicDownloader(
        base_downloader,
        song_downloader,
        music_video_downloader,
        uploaded_video_downloader,
    )

    # Download a song by URL
    url_info = downloader.get_url_info(
        "https://music.apple.com/us/album/never-gonna-give-you-up-2022-remaster/1624945511?i=1624945512"
    )
    if url_info:
        download_queue = await downloader.get_download_queue(url_info)
        if download_queue:
            for download_item in download_queue:
                await downloader.download(download_item)


if __name__ == "__main__":
    asyncio.run(main())
```
