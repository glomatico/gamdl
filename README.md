# Glomatico's ✨ Apple Music ✨ Downloader
A Python script to download Apple Music songs/music videos/albums/playlists.

![Windows CMD usage example](https://i.imgur.com/18Azlg4.png)

This is a rework of https://github.com/loveyoursupport/AppleMusic-Downloader/tree/661a274d62586b521feec5a7de6bee0e230fdb7d.

Some new features that I added:
* MP4Box for muxing
* Tags for music videos
* Multiple URLs input
* iTunes folder structure
* Embedded lyrics and .lrc file
* Auto set region
* Playlist support
* And much more!

## Setup
1. Install Python 3.7 or newer
2. Install gamdl with pip
    ```
    pip install gamdl
    ```
3. Add MP4Box and mp4decrypt to your PATH
    * You can get them from here:
        * MP4Box: https://gpac.wp.imt.fr/downloads/
        * mp4decrypt: https://www.bento4.com/downloads/
4. Export your Apple Music cookies as `cookies.txt` to the same folder that you will run the script
    * You can export your cookies by using this Google Chrome extension on Apple Music website: https://chrome.google.com/webstore/detail/open-cookiestxt/gdocmgbfkjnnpapoeobnolbbkoibbcif. Make sure to be logged in.
5. Put your Widevine Device file (.wvd) in the same folder that you will run the script
    * You can use Dumper to dump your phone's L3 CDM: https://github.com/Diazole/dumper. Once you have the L3 CDM, you can use pywidevine to create the .wvd file from it.
        1. Install pywidevine with pip
            ```
            pip install pywidevine pyyaml
            ```
        2. Create the .wvd file
            ```
            pywidevine create-device -t ANDROID -l 3 -k private_key.pem -c client_id.bin -o .
            ```
6. (optional) Add aria2c to your PATH for faster downloads
    * You can get it from here: https://github.com/aria2/aria2/releases.

## Usage
```
usage: gamdl [-h] [-u [URLS_TXT]] [-w WVD_LOCATION] [-f FINAL_PATH] [-t TEMP_PATH] [-c COOKIES_LOCATION] [-m]
                   [-p] [-a] [-o] [-n] [-s] [-e] [-i] [-v]
                   [url ...]

Download Apple Music songs/music videos/albums/playlists

positional arguments:
  url                   Apple Music song/music video/album/playlist URL(s) (default: None)

options:
  -h, --help            show this help message and exit
  -u [URLS_TXT], --urls-txt [URLS_TXT]
                        Read URLs from a text file (default: None)
  -w WVD_LOCATION, --wvd-location WVD_LOCATION
                        .wvd file location (default: *.wvd)
  -f FINAL_PATH, --final-path FINAL_PATH
                        Final Path (default: Apple Music)
  -t TEMP_PATH, --temp-path TEMP_PATH
                        Temp Path (default: temp)
  -c COOKIES_LOCATION, --cookies-location COOKIES_LOCATION
                        Cookies location (default: cookies.txt)
  -m, --disable-music-video-skip
                        Disable music video skip on playlists/albums (default: False)
  -p, --prefer-hevc     Prefer HEVC over AVC (default: False)
  -a, --heaac           Download songs/music videos with HE-AAC instead of AAC (default: False)
  -o, --overwrite       Overwrite existing files (default: False)
  -n, --no-lrc          Don't create .lrc file (default: False)
  -s, --skip-cleanup    Skip cleanup (default: False)
  -e, --print-exceptions
                        Print execeptions (default: False)
  -i, --print-video-m3u8-url
                        Print Video M3U8 URL (default: False)
  -v, --version         show program's version number and exit
```

## Songs/Music Videos quality
* Songs:
    * 256kbps AAC / HE-AAC 64kbps
* Music Videos (varies depending on the video):
    * 4K HEVC 20mbps, AAC 256kbps / HE-AAC 64kbps
    * 4K HEVC 12mbps, AAC 256kbps / HE-AAC 64kbps
    * 1080p AVC 10mbps, AAC 256kbps / HE-AAC 64kbps
    * 1080p AVC 6.5bps, AAC 256kbps / HE-AAC 64kbps
    * 720p AVC 4mbps, AAC 256kbps / HE-AAC 64kbps
    * 576p AVC 2mbps, AAC 256kbps / HE-AAC 64kbps
    * 480p AVC 1.5mbps, AAC 256kbps / HE-AAC 64kbps
    * 360p AVC 1mbps, AAC 256kbps / HE-AAC 64kbps

Some videos may include EIA-608 closed captions.
