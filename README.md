# gamdl - Glomatico's Apple Music Downloader
A Python script to download Apple Music songs/music videos/albums/playlists. This is a rework of https://github.com/loveyoursupport/AppleMusic-Downloader/tree/661a274d62586b521feec5a7de6bee0e230fdb7d.

## Features
* Download songs in 256kbps AAC or in 64kbps HE-AAC
* Download music videos up to 4K
* Download synced lyrics
* Choose between FFmpeg and MP4Box for remuxing
* Choose between yt-dlp and N_m3u8DL-RE for downloading
* Highly customizable
  
## Installation
1. Install Python 3.7 or higher
2. Add [FFmpeg](https://ffmpeg.org/download.html) and [mp4decrypt](https://www.bento4.com/downloads/) to PATH
    * mp4decrypt is only needed if you want to download music videos
3. Place your cookies in the same folder that you will run gamdl as `cookies.txt`
    * You can export your cookies by using this Google Chrome extension on Apple Music website: https://chrome.google.com/webstore/detail/open-cookiestxt/gdocmgbfkjnnpapoeobnolbbkoibbcif. Make sure to be logged in.
4. Place your .wvd file in the same folder that you will run gamdl as `device.wvd`
    * To get a .wvd file, you can use [dumper](https://github.com/wvdumper/dumper) to dump a L3 CDM from an Android device. Once you have the L3 CDM, use pywidevine to create the .wvd file from it.
        1. Install pywidevine with pip
            ```bash
            pip install pywidevine pyyaml
            ```
        2. Create the .wvd file
            ```bash
            pywidevine create-device -t ANDROID -l 3 -k private_key.pem -c client_id.bin -o .
            ```
5. Install gamdl using pip
    ```bash
    pip install gamdl
    ```

## Examples
* Download a song
    ```bash
    gamdl "https://music.apple.com/us/album/never-gonna-give-you-up-2022-remaster/1626265761?i=1626265765"
    ```
* Download an album
    ```bash
    gamdl "https://music.apple.com/us/album/whenever-you-need-somebody-2022-remaster/1626265761"
    ```

## Configuration
You can configure gamdl by using the command line arguments or the config file. The config file is created automatically when you run gamdl for the first time at `~/.gamdl/config.json` on Linux and `%USERPROFILE%\.gamdl\config.json` on Windows. Config file values can be overridden using command line arguments.
| Command line argument / Config file key                         | Description                                                            | Default value                      |
| --------------------------------------------------------------- | ---------------------------------------------------------------------- | ---------------------------------- |
| `-f`, `--final-path` / `final_path`                             | Path where the downloaded files will be saved.                         | `./Apple Music`                    |
| `-t`, `--temp-path` / `temp_path`                               | Path where the temporary files will be saved.                          | `./temp`                           |
| `-c`, `--cookies-location` / `cookies_location`                 | Location of the cookies file.                                          | `./cookies.txt`                    |
| `-w`, `--wvd-location` / `wvd_location`                         | Location of the .wvd file.                                             | `./device.wvd`                     |
| `--ffmpeg-location` / `ffmpeg_location`                         | Location of the FFmpeg binary.                                         | `ffmpeg`                           |
| `--mp4box-location` / `mp4box_location`                         | Location of the MP4Box binary.                                         | `MP4Box`                           |
| `--mp4decrypt-location` / `mp4decrypt_location`                 | Location of the mp4decrypt binary.                                     | `mp4decrypt`                       |
| `--nm3u8dlre-location` / `nm3u8dlre_location`                   | Location of the N_m3u8DL-RE binary.                                    | `N_m3u8DL-RE`                      |
| `--config-location` / -                                         | Location of the config file.                                           | `<home_folder>/.gamdl/config.json` |
| `--template-folder-album` / `template_folder_album`             | Template of the album folders as a format string.                      | `{album_artist}/{album}`           |
| `--template-folder-compilation` / `template_folder_compilation` | Template of the compilation album folders as a format string.          | `Compilations/{album}`             |
| `--template-file-single-disc` / `template_file_single_disc`     | Template of the track files for single-disc albums as a format string. | `{track:02d} {title}`              |
| `--template-file-multi-disc` / `template_file_multi_disc`       | Template of the track files for multi-disc albums as a format string.  | `{disc}-{track:02d} {title}`       |
| `--template-folder-music-video` / `template_folder_music_video` | Template of the music video folders as a format string.                | `{artist}/Unknown Album`           |
| `--template-file-music-video` / `template_file_music_video`     | Template of the music video files as a format string.                  | `{title}`                          |
| `--cover-size` / `cover_size`                                   | Size of the cover.                                                     | `1200`                             |
| `--cover-format` / `cover_format`                               | Format of the cover.                                                   | `jpg`                              |
| `--remux-mode` / `remux_mode`                                   | Remux mode.                                                            | `ffmpeg`                           |
| `--download-mode` / `download_mode`                             | Download mode.                                                         | `ytdlp`                            |
| `-e`, `--exclude-tags` / `exclude_tags`                         | List of tags to exclude from file tagging separated by commas.         | `null`                             |
| `--truncate` / `truncate`                                       | Maximum length of the file/folder names.                               | `40`                               |
| `-l`, `--log-level` / `log_level`                               | Log level.                                                             | `INFO`                             |
| `--prefer-hevc` / `prefer_hevc`                                 | Prefer HEVC over AVC when downloading music videos.                    | `false`                            |
| `--ask-video-format` / `ask_video_format`                       | Ask for the video format when downloading music videos.                | `false`                            |
| `--disable-music-video-skip` / `disable_music_video_skip`       | Don't skip downloading music videos in albums/playlists.               | `false`                            |
| `-l`, `--lrc-only` / `lrc_only`                                 | Download only the synced lyrics.                                       | `false`                            |
| `-n`, `--no-lrc` / `no_lrc`                                     | Don't download the synced lyrics.                                      | `false`                            |
| `-s`, `--save-cover` / `save_cover`                             | Save cover as a separate file.                                         | `false`                            |
| `--songs-heaac` / `songs_heaac`                                 | Download songs in HE-AAC 64kbps.                                       | `false`                            |
| `-o`, `--overwrite` / `overwrite`                               | Overwrite existing files.                                              | `false`                            |
| `--print-exceptions` / `print_exceptions`                       | Print exceptions.                                                      | `false`                            |
| `-u`, `--url-txt` / -                                           | Read URLs as location of text files containing URLs.                   | `false`                            |
| `-n`, `--no-config-file` / -                                    | Don't use the config file.                                             | `false`                            |

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
  
### Remux mode
The following remux modes are available:
* `ffmpeg`
    * Can decrypt and remux songs but can't decrypt music videos by itself
    * Decryption may not work on older versions of FFmpeg
* `mp4box`
    * Requires mp4decrypt
    * Doesn't convert closed captions in music videos that have them
    * Can be obtained from here: https://gpac.wp.imt.fr/downloads

### Download mode
The following download modes are available:
* `ytdlp`
* `nm3u8dlre`
    * Faster than `ytdlp`
    * Requires FFmpeg
    * Can be obtained from here: https://github.com/nilaoda/N_m3u8DL-RE/releases

## Music videos quality
Music videos will be downloaded in the highest quality available by default. The available qualities are:
* AVC 1080p 10mbps, AAC 256kbps
* AVC 1080p 6.5mbps, AAC 256kbps
* AVC 720p 4mbps, AAC 256kbps
* AVC 576p 2mbps, AAC 256kbps
* AVC 480p 1.5mbps, AAC 256kbps
* AVC 360p 1mbps, AAC 256kbps

By enabling the `prefer_hevc` option, music videos will be downloaded in the highest HEVC quality available. The available qualities are:
* HEVC 4K 20mbps, AAC 256kbps
* HEVC 4K 12mbps, AAC 256kbps

Enable `ask_video_format` to select a custom audio/video format.
