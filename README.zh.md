# Glomatico 苹果音乐下载器
一个用于下载苹果音乐歌曲/音乐视频/专辑/播放列表/帖子的 Python 命令行应用程序。

**Discord 频道:** [https://discord.gg/aBjMEZ9tnq](https://discord.gg/aBjMEZ9tnq)

## 特点
* 支持 AAC/Spatial AAC/Dolby Atmos/ALAC* 格式的歌曲下载
* 支持高达 4K 的音乐视频下载
* 下载 LRC、SRT 或 TTML 格式的同步歌词
* 可选择使用 FFmpeg 或 MP4Box 进行重新混流
* 可选择使用 yt-dlp 或 N_m3u8DL-RE 进行下载
* 高度可定制化
* 使用艺术家链接下载其全部专辑或音乐视频

## 先决条件
* Python 版本 3.8 或更高
* 您的 Apple Music 帐户的 cookies 文件（需要已订阅）
    * 您可以使用以下浏览器扩展之一在已登录帐户的 Apple Music 网站上获取您的 cookies：
        * Firefox: [https://addons.mozilla.org/addon/export-cookies-txt](https://addons.mozilla.org/addon/export-cookies-txt)
        * 基于 Chromium 的浏览器：[https://chrome.google.com/webstore/detail/gdocmgbfkjnnpapoeobnolbbkoibbcif](https://chrome.google.com/webstore/detail/gdocmgbfkjnnpapoeobnolbbkoibbcif)
* 系统 PATH 中有 FFmpeg
    * 旧版本的 FFmpeg 可能无法工作。
    * 最新的二进制文件可从以下链接获取：
        * Windows: [https://github.com/AnimMouse/ffmpeg-stable-autobuild/releases](https://github.com/AnimMouse/ffmpeg-stable-autobuild/releases)
        * Linux: [https://johnvansickle.com/ffmpeg/](https://johnvansickle.com/ffmpeg/)
* （可选）系统 PATH 中有 mp4decrypt
    * 用于下载非传统格式的音乐视频和歌曲。
    * 可从此处获取二进制文件：[https://www.bento4.com/downloads/](https://www.bento4.com/downloads/)

## 安装
1. 使用 pip 安装 `gamdl` 包
    ```bash
    pip install gamdl
    ```
2. 将您的 cookies 文件放置在您将运行 gamdl 的目录中，并命名为 `cookies.txt`。

## 使用
```bash
gamdl [OPTIONS] URLS...
```

### 示例
* 下载一首歌曲
    ```bash
    gamdl "https://music.apple.com/us/album/never-gonna-give-you-up-2022-remaster/1624945511?i=1624945512"
    ```
* 下载一个专辑
    ```bash
    gamdl "https://music.apple.com/us/album/whenever-you-need-somebody-2022-remaster/1624945511"
    ```
* 选择要从艺术家处下载的专辑或音乐视频
    ```bash
    gamdl "https://music.apple.com/us/artist/rick-astley/669771"
    ```
    * 按空格键选择或取消选择，按 Ctrl + A 选择全部。

## 配置
gamdl 可以通过命令行参数或配置文件进行配置。配置文件在第一次运行 gamdl 时会自动创建，位于 Linux 上的 `~/.gamdl/config.json` 和 Windows 上的 `%USERPROFILE%\.gamdl\config.json`。配置文件的值可以使用命令行参数进行覆盖。
| 命令行参数 / 配置文件键                         | 描述                                                   | 默认值                                             |
| -------------------------------------------------- | ------------------------------------------------------ | -------------------------------------------------- |
| `--disable-music-video-skip` / `disable_music_video_skip` | 不跳过下载专辑/播放列表中的音乐视频。                  | `false`                                            |
| `--save-cover`, `-s` / `save_cover`               | 将封面保存为单独的文件。                                | `false`                                            |
| `--overwrite` / `overwrite`                       | 覆盖现有文件。                                         | `false`                                            |
| `--read-urls-as-txt`, `-r` / -                    | 将 URL 解释为包含以新行分隔的 URL 的文本文件的路径。   | `false`                                            |
| `--synced-lyrics-only` / `synced_lyrics_only`     | 仅下载同步歌词。                                       | `false`                                            |
| `--no-synced-lyrics` / `no_synced_lyrics`         | 不下载同步歌词。                                       | `false`                                            |
| `--config-path` / -                               | 配置文件路径。                                         | `<home>/.spotify-web-downloader/config.json`       |
| `--log-level` / `log_level`                       | 日志级别。                                             | `INFO`                                             |
| `--print-exceptions` / `print_exceptions`         | 打印异常。                                             | `false`                                            |
| `--cookies-path`, `-c` / `cookies_path`           | .txt cookies 文件的路径。                               | `./cookies.txt`                                   |
| `--language`, `-l` / `language`                   | 元数据语言，作为 ISO-2A 语言代码（不一定适用于视频）。| `en-US`                                            |
| `--output-path`, `-o` / `output_path`             | 输出目录路径。                                         | `./Apple Music`                                   |
| `--temp-path` / `temp_path`                       | 临时目录路径。                                         | `./temp`                                           |
| `--wvd-path` / `wvd_path`                         | .wvd 文件路径。                                        | `null`                                             |
| `--nm3u8dlre-path` / `nm3u8dlre_path`             | N_m3u8DL-RE 二进制文件路径。                            | `N_m3u8dl-RE`                                      |
| `--mp4decrypt-path` / `mp4decrypt_path`           | mp4decrypt 二进制文件路径。                             | `mp4decrypt`                                       |
| `--ffmpeg-path` / `ffmpeg_path`                   | FFmpeg 二进制文件路径。                                | `ffmpeg`                                           |
|`--mp4box-path` / `mp4box_path`                 | MP4Box 二进制文件路径。                                | `MP4Box`                                           |
| `--download-mode` / `download_mode`               | 下载模式。                                             | `ytdlp`                                            |
| `--remux-mode` / `remux_mode`                     | 重新混流模式。                                         | `ffmpeg`                                           |
| `--cover-format` / `cover_format`                 | 封面格式。                                             | `jpg`                                              |
| `--template-folder-album` / `template_folder_album` | 专辑中的曲目模板文件夹。                              | `{album_artist}/{album}`                          |
| `--template-folder-compilation` / `template_folder_compilation` | 编译专辑中的曲目模板文件夹。               | `Compilations/{album}`                            |
| `--template-file-single-disc` / `template_file_single_disc` | 单碟专辑中的曲目模板文件。                  | `{track:02d} {title}`                             |
| `--template-file-multi-disc` / `template_file_multi_disc` | 多碟专辑中的曲目模板文件。                  | `{disc}-{track:02d} {title}`                      |
| `--template-folder-no-album` / `template_folder_no_album` | 无专辑曲目的模板文件夹。                       | `{artist}/Unknown Album`                          |
| `--template-file-no-album` / `template_file_no_album` | 无专辑曲目的模板文件。                         | `{title}`                                          |
| `--template-date` / `template_date`               | 日期标签模板。                                         | `%Y-%m-%dT%H:%M:%SZ`                              |
| `--exclude-tags` / `exclude_tags`                 | 要排除的逗号分隔标签。                                 | `null`                                             |
| `--cover-size` / `cover_size`                     | 封面尺寸。                                             | `1200`                                             |
| `--truncate` / `truncate`                         | 文件/文件夹名称的最大长度。                             | `40`                                               |
| `--codec-song` / `codec_song`                     | 歌曲编解码器。                                         | `aac-legacy`                                       |
| `--synced-lyrics-format` / `synced_lyrics_format` | 同步歌词格式。                                         | `lrc`                                              |
| `--codec-music-video` / `codec_music_video`       | 音乐视频编解码器。                                     | `h264`                                             |
| `--quality-post` / `quality_post`                 | 帖子视频质量。                                         | `best`                                             |
| `--no-config-file`, `-n` / -                      | 不使用配置文件。                                       | `false`                                            |


### 标签变量
以下变量可用于模板文件夹/文件和/或`exclude_tags`列表中：
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

### 重新混流模式
以下重新混流模式可用：
* `ffmpeg`
    * 可仅用于歌曲和使用传统歌曲编解码器时，无需 mp4decrypt
* `mp4box`
    * 需要 mp4decrypt
    * 不会转换音乐视频中的闭合字幕
    * 可从此处获取：[https://gpac.wp.imt.fr/downloads](https://gpac.wp.imt.fr/downloads)

### 下载模式
以下下载模式可用：
* `ytdlp`
* `nm3u8dlre`
    * 比 `ytdlp` 更快
    * 需要 FFmpeg
    * 可从此处获取：[https://github.com/nilaoda/N_m3u8DL-RE/releases](https://github.com/nilaoda/N_m3u8DL-RE/releases)


### 歌曲编解码器
以下编解码器可用：
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
    * 使用此选项时，gamdl 将询问您要使用的可用于歌曲的**非传统**编解码器。

**不保证对非传统编解码器的支持，因为大多数歌曲在使用非传统编解码器时无法下载。**

### 音乐视频编解码器
以下编解码器可用：
* `h264`（高达 1080p，带 AAC 256kbps）
* `h265`（高达 2160p，带 AAC 256kbps）
* `ask`
    * 使用此选项时，gamdl 将询问您要使用的可用于音乐视频的音频和视频编解码器。

### 帖子视频/额外视频质量
以下质量可用：
* `best`（高达 1080p，带 AAC 256kbps）
* `ask`
    * 使用此选项时，gamdl 将询问您要使用的可用于视频的视频质量。

帖子视频不需要重新混流，并且仅限于 `ytdlp` 下载模式。

### 同步歌词格式
以下同步歌词格式可用：
* `lrc`
* `srt`
* `ttml`
    * Apple Music 同步歌词的原生格式。
    * 媒体播放器对其支持程度较低。

### 封面格式
以下封面格式可用：
* `jpg`
* `png`
