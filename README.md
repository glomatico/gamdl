# Glomatico's Apple Music Downloader
A Python CLI app for downloading Apple Music songs/music videos/posts.

**Discord Server:** https://discord.gg/aBjMEZ9tnq

## Features
* Download songs in AAC/Spatial AAC/Dolby Atmos/ALAC*
* Download music videos up to 4K
* Download synced lyrics in LRC, SRT or TTML
* Choose between FFmpeg and MP4Box for remuxing
* Choose between yt-dlp and N_m3u8DL-RE for downloading
* Highly customizable
* Use artist links to download all of their albums or music videos

## Prerequisites
* Python 3.8 or higher
* The cookies file of your Apple Music browser session (requires an active subscription)
    * You can get your cookies by using one of the following extensions on your browser of choice at the Apple Music website with your account signed in:
        * Firefox: https://addons.mozilla.org/addon/export-cookies-txt
        * Chromium based browsers: https://chrome.google.com/webstore/detail/gdocmgbfkjnnpapoeobnolbbkoibbcif
* FFmpeg on your system PATH
    * Older versions of FFmpeg may not work.
    * Up to date binaries can be obtained from the links below:
        * Windows: https://github.com/AnimMouse/ffmpeg-stable-autobuild/releases
        * Linux: https://johnvansickle.com/ffmpeg/
* (Optional) mp4decrypt on your system PATH
    * Required to download music videos and songs in non-legacy formats.
    * Binaries can be obtained from here: https://www.bento4.com/downloads/.
 
## Installation
1. Install the package `gamdl` using pip
    ```bash
    pip install gamdl
    ```
2. Place your cookies file in the directory from which you will be running gamdl and name it `cookies.txt`.

## Usage
```bash
gamdl [OPTIONS] URLS...
```

### Examples
* Download a song
    ```bash
    gamdl "https://music.apple.com/us/album/never-gonna-give-you-up-2022-remaster/1624945511?i=1624945512"
    ```
* Download an album
    ```bash
    gamdl "https://music.apple.com/us/album/whenever-you-need-somebody-2022-remaster/1624945511"
    ```
* Choose which albums or music videos to download from an artist
    ```bash
    gamdl "https://music.apple.com/us/artist/rick-astley/669771"
    ```

### Interactive prompt controls
* Arrow keys - Move selection
* Space - Toggle selection
* Ctrl + A - Select all
* Enter - Confirm selection

## Configuration
gamdl can be configured by using the command line arguments or the config file. The config file is created automatically when you run gamdl for the first time at `~/.gamdl/config.json` on Linux and `%USERPROFILE%\.gamdl\config.json` on Windows. Config file values can be overridden using command line arguments.
| Command line argument / Config file key                         | Description                                                                  | Default value                                |
| --------------------------------------------------------------- | ---------------------------------------------------------------------------- | -------------------------------------------- |
| `--disable-music-video-skip` / `disable_music_video_skip`       | Don't skip downloading music videos in albums/playlists.                     | `false`                                      |
| `--save-cover`, `-s` / `save_cover`                             | Save cover as a separate file.                                               | `false`                                      |
| `--overwrite` / `overwrite`                                     | Overwrite existing files.                                                    | `false`                                      |
| `--read-urls-as-txt`, `-r` / -                                  | Interpret URLs as paths to text files containing URLs separated by newlines. | `false`                                      |
| `--synced-lyrics-only` / `synced_lyrics_only`                   | Download only the synced lyrics.                                             | `false`                                      |
| `--no-synced-lyrics` / `no_synced_lyrics`                       | Don't download the synced lyrics.                                            | `false`                                      |
| `--config-path` / -                                             | Path to config file.                                                         | `<home>/.spotify-web-downloader/config.json` |
| `--log-level` / `log_level`                                     | Log level.                                                                   | `INFO`                                       |
| `--print-exceptions` / `print_exceptions`                       | Print exceptions.                                                            | `false`                                      |
| `--cookies-path`, `-c` / `cookies_path`                         | Path to .txt cookies file.                                                   | `./cookies.txt`                              |
| `--language`, `-l` / `language`                                 | Metadata language as an ISO-2A language code (don't always work for videos). | `en-US`                                      |
| `--output-path`, `-o` / `output_path`                           | Path to output directory.                                                    | `./Apple Music`                              |
| `--temp-path` / `temp_path`                                     | Path to temporary directory.                                                 | `./temp`                                     |
| `--wvd-path` / `wvd_path`                                       | Path to .wvd file.                                                           | `null`                                       |
| `--nm3u8dlre-path` / `nm3u8dlre_path`                           | Path to N_m3u8DL-RE binary.                                                  | `N_m3u8DL-RE`                                |
| `--mp4decrypt-path` / `mp4decrypt_path`                         | Path to mp4decrypt binary.                                                   | `mp4decrypt`                                 |
| `--ffmpeg-path` / `ffmpeg_path`                                 | Path to FFmpeg binary.                                                       | `ffmpeg`                                     |
| `--mp4box-path` / `mp4box_path`                                 | Path to MP4Box binary.                                                       | `MP4Box`                                     |
| `--download-mode` / `download_mode`                             | Download mode.                                                               | `ytdlp`                                      |
| `--remux-mode` / `remux_mode`                                   | Remux mode.                                                                  | `ffmpeg`                                     |
| `--cover-format` / `cover_format`                               | Cover format.                                                                | `jpg`                                        |
| `--template-folder-album` / `template_folder_album`             | Template folder for tracks that are part of an album.                        | `{album_artist}/{album}`                     |
| `--template-folder-compilation` / `template_folder_compilation` | Template folder for tracks that are part of a compilation album.             | `Compilations/{album}`                       |
| `--template-file-single-disc` / `template_file_single_disc`     | Template file for the tracks that are part of a single-disc album.           | `{track:02d} {title}`                        |
| `--template-file-multi-disc` / `template_file_multi_disc`       | Template file for the tracks that are part of a multi-disc album.            | `{disc}-{track:02d} {title}`                 |
| `--template-folder-no-album` / `template_folder_no_album`       | Template folder for the tracks that are not part of an album.                | `{artist}/Unknown Album`                     |
| `--template-file-no-album` / `template_file_no_album`           | Template file for the tracks that are not part of an album.                  | `{title}`                                    |
| `--template-date` / `template_date`                             | Date tag template.                                                           | `%Y-%m-%dT%H:%M:%SZ`                         |
| `--exclude-tags` / `exclude_tags`                               | Comma-separated tags to exclude.                                             | `null`                                       |
| `--cover-size` / `cover_size`                                   | Cover size.                                                                  | `1200`                                       |
| `--truncate` / `truncate`                                       | Maximum length of the file/folder names.                                     | `40`                                         |
| `--codec-song` / `codec_song`                                   | Song codec.                                                                  | `aac-legacy`                                 |
| `--synced-lyrics-format` / `synced_lyrics_format`               | Synced lyrics format.                                                        | `lrc`                                        |
| `--codec-music-video` / `codec_music_video`                     | Music video codec.                                                           | `h264`                                       |
| `--quality-post` / `quality_post`                               | Post video quality.                                                          | `best`                                       |
| `--no-config-file`, `-n` / -                                    | Do not use a config file.                                                    | `false`                                      |


### Tags variables
The following variables can be used in the template folders/files and/or in the `exclude_tags` list:
* `album`
* `album_artist`
* `album_id`
* `album_sort`
* `artist`
* `artist_id`
* `artist_sort`
* `comment`
* `compilation`
* `composer`
* `composer_id`
* `composer_sort`
* `copyright`
* `cover`
* `date`
* `disc`
* `disc_total`
* `gapless`
* `genre`
* `genre_id`
* `lyrics`
* `media_type`
* `rating`
* `storefront`
* `title`
* `title_id`
* `title_sort`
* `track`
* `track_total`
* `xid`

### Remux modes
The following remux modes are available:
* `ffmpeg`
    * Can be used without mp4decrypt only for songs and when using legacy song codecs
* `mp4box`
    * Requires mp4decrypt
    * Doesn't convert closed captions in music videos that have them
    * Can be obtained from here: https://gpac.wp.imt.fr/downloads

### Download modes
The following download modes are available:
* `ytdlp`
* `nm3u8dlre`
    * Faster than `ytdlp`
    * Requires FFmpeg
    * Can be obtained from here: https://github.com/nilaoda/N_m3u8DL-RE/releases


### Song codecs
The following codecs are available:
* `aac-legacy`
* `aac-he-legacy`
* `aac`
* `aac-he`
* `aac-binaural`
* `aac-downmix`
* `aac-he-binaural`
* `aac-he-downmix`
* `atmos`
* `ac3`
* `alac`
* `ask`
    * When using this option, gamdl will ask you which **non-legacy** codec to use that is available for the song.

**Support for non-legacy codecs are not guaranteed, as most of the songs cannot be downloaded when using non-legacy codecs.**

### Music videos codecs
The following codecs are available:
* `h264` (up to 1080p, with AAC 256kbps)
* `h265` (up to 2160p, with AAC 256kpbs)
* `ask`
    * When using this option, gamdl will ask you which audio and video codec to use that is available for the music video.
  
### Post videos/extra videos qualities
The following qualities are available:
* `best` (up to 1080p, with AAC 256kbps)
* `ask`
    * When using this option, gamdl will ask you which video quality to use that is available for the video.

Post videos doesn't require remuxing and are limited to `ytdlp` download mode.

### Synced lyrics formats
The following synced lyrics formats are available:
* `lrc`
* `srt`
* `ttml`
    * Native format for Apple Music synced lyrics.
    * Highly unsupported by most media players.
  
### Cover formats
The following cover formats are available:
* `jpg`
* `png`
