# Glomatico's ✨ Apple Music ✨ Downloader
A Python script to download Apple Music songs/music videos/albums/playlists. This is a rework of https://github.com/loveyoursupport/AppleMusic-Downloader/tree/661a274d62586b521feec5a7de6bee0e230fdb7d.

## Setup
1. Install Python 3.7 or newer
2. Install gamdl with pip
    ```
    pip install gamdl
    ```
3. Add [FFmpeg](https://ffmpeg.org/download.html) and [mp4decrypt](https://www.bento4.com/downloads/) to PATH or specify the location using the command line arguments or the config file (see [Configuration](#configuration))
   * mp4decrypt is only needed if you want to download music videos
4. Place your cookies in the same folder that you will run the script as `cookies.txt`
    * You can export your cookies by using this Google Chrome extension on Apple Music website: https://chrome.google.com/webstore/detail/open-cookiestxt/gdocmgbfkjnnpapoeobnolbbkoibbcif. Make sure to be logged in.
5. Place your .wvd file in the same folder that you will run the script as `device.wvd`
    * You can use [dumper](https://github.com/wvdumper/dumper) to dump your phone's L3 CDM. Once you have the L3 CDM, use pywidevine to create the .wvd file from it.
        1. Install pywidevine with pip
            ```
            pip install pywidevine pyyaml
            ```
        2. Create the .wvd file
            ```
            pywidevine create-device -t ANDROID -l 3 -k private_key.pem -c client_id.bin -o .
            ```

## Examples
* Download a song
    ```
    gamdl https://music.apple.com/us/album/never-gonna-give-you-up-2022-remaster/1626265761?i=1626265765
    ```
* Download an album
    ```
    gamdl https://music.apple.com/us/album/whenever-you-need-somebody-2022-remaster/1626265761
    ```

## Configuration
gamdl can be configured using the command line arguments or the config file. The config file is created automatically when you run gamdl for the first time at `~/.gamdl/config.json` on Linux and `%USERPROFILE%\.gamdl\config.json` on Windows. Config file values can be overridden using command line arguments.

| Command line argument / Config file key | Description | Default value |
| --- | --- | --- |
| `-f`, `--final-path` / `final_path` | Path where the downloaded files will be saved. | `./Apple Music` |
| `-t`, `--temp-path` / `temp_path` | Path where the temporary files will be saved. | `./temp` |
| `-c`, `--cookies-location` / `cookies_location` | Location of the cookies file. | `./cookies.txt` |
| `-w`, `--wvd-location` / `wvd_location` | Location of the .wvd file. | `./device.wvd` |
| `--ffmpeg-location` / `ffmpeg_location` | Location of the FFmpeg binary. | `ffmpeg` |
| `--mp4box-location` / `mp4box_location` | Location of the MP4Box binary. | `MP4Box` |
| `--mp4decrypt-location` / `mp4decrypt_location` | Location of the mp4decrypt binary. | `mp4decrypt` |
| `--nm3u8dlre-location` / `nm3u8dlre_location` | Location of the N_m3u8DL-RE binary. | `N_m3u8DL-RE` |
| `--config-location` / - | Location of the config file. | `<home_folder>/.gamdl/config.json` |
| `--template-folder-album` / `template_folder_album` | Template of the album folders as a format string. | `{album_artist}/{album}` |
| `--template-folder-compilation` / `template_folder_compilation` | Template of the compilation album folders as a format string. | `Compilations/{album}` |
| `--template-file-single-disc` / `template_file_single_disc` | Template of the track files for single-disc albums as a format string. | `{track:02d} {title}` |
| `--template-file-multi-disc` / `template_file_multi_disc` | Template of the track files for multi-disc albums as a format string. | `{disc}-{track:02d} {title}` |
| `--template-folder-music-video` / `template_folder_music_video` | Template of the music video folders as a format string. | `{artist}/Unknown Album` |
| `--template-file-music-video` / `template_file_music_video` | Template of the music video files as a format string. | `{title}` |
| `--cover-size` / `cover_size` | Size of the cover. | `1200` |
| `--cover-format` / `cover_format` | Format of the cover. | `jpg` |
| `--remux-mode` / `remux_mode` | Remuxing mode. | `ffmpeg` |
| `--download-mode` / `download_mode` | Download mode. | `yt-dlp` |
| `-e`, `--exclude-tags` / `exclude_tags` | List of tags to exclude from file tagging separated by commas. | `null` |
| `--truncate` / `truncate` | Maximum length of the file/folder names. | `40` |
| `-l`, `--log-level` / `log_level` | Log level. | `INFO` |
| `--prefer-hevc` / `prefer_hevc` | Prefer HEVC over AVC when downloading music videos. | `false` |
| `--ask-video-format` / `ask_video_format` | Ask for the video format when downloading music videos. | `false` |
| `--disable-music-video-album-skip` / `disable_music_video_album_skip` | Don't skip downloading music videos in albums. | `false` |
| `-l`, `--lrc-only` / `lrc_only` | Download only the synced lyrics. | `false` |
| `-n`, `--no-lrc` / `no_lrc` | Don't download the synced lyrics. | `false` |
| `-s`, `--save-cover` / `save_cover` | Save cover as a separate file. | `false` |
| `--songs-heaac` / `songs_heaac` | Download songs in 64kbps HE-AAC. | `false` |
| `-o`, `--overwrite` / `overwrite` | Overwrite existing files. | `false` |
| `--print-exceptions` / `print_exceptions` | Print exceptions. | `false` |
| `-u`, `--url-txt` / - | Read URLs as location of text files containing URLs. | `false` |
| `-n`, `--no-config-file` / - | Don't use the config file. | `false` |

### Tags variables
The following variables can be used in the template folder/file and/or in the exclude_tags list:
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
* `disc`
* `disc_total`
* `gapless`
* `genre`
* `genre_id`
* `lyrics`
* `media_type`
* `rating`
* `release_date`
* `storefront`
* `title`
* `title_id`
* `title_sort`
* `track`
* `track_total`
* `xid`
  
### Remux mode
Can be either `ffmpeg` or `mp4box`. `mp4decrypt` is required for music videos and remuxing with `mp4box`. `mp4box` keeps the closed captions track present in some music videos.

### Download mode
Can be either `yt-dlp` or `nm3u8dlre`.
