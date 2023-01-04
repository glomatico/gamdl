# Glomatico's ✨ Apple Music ✨ Downloader
A Python script to download Apple Music songs/music videos/albums/playlists.

![Windows CMD usage example](https://i.imgur.com/byjqmGF.png)

This is a rework of https://github.com/loveyoursupport/AppleMusic-Downloader/tree/661a274d62586b521feec5a7de6bee0e230fdb7d.

Some new features that I added:
- MP4Box for muxing
- Tags for music videos
- Multiple URLs input
- iTunes folder structure
- Embedded lyrics and .lrc file
- Auto set region
- Playlist support
- And much more!

## Setup
1. Install Python 3.8 or higher
2. Install the required packages using pip: 
    ```
    pip install -r requirements.txt
    ```
3. Add MP4Box and mp4decrypt to your PATH
    * You can get them from here:
        * MP4Box: https://gpac.wp.imt.fr/downloads/
        * mp4decrypt: https://www.bento4.com/downloads/
4. Export your Apple Music cookies as `cookies.txt` and put it in the same folder as the script
    * You can export your cookies by using this Google Chrome extension on Apple Music website: https://chrome.google.com/webstore/detail/cookies-txt/njabckikapfpffapmjgojcnbfjonfjfg. Make sure to be logged in.
5. Put your L3 Widevine Keys (`device_client_id_blob` and `device_private_key` files) on `./pywidevine/L3/cdm/devices/android_generic` folder
    * You can get your L3 Widevine Keys by using Dumper: https://github.com/Diazole/dumper
        * The generated `private_key.pem` and `client_id.bin` files should be renamed to `device_private_key` and `device_client_id_blob` respectively.
6. (optional) Add aria2c to your PATH for faster downloads
    * You can get it from here: https://aria2.github.io/.

## Usage
```
python gamdl.py [OPTIONS] [URLS]
```
Tracks are saved in `./Apple Music` by default, but the directory can be changed using `--final-path` argument.

Use `--help` argument to see all available options.

## Songs/Music Videos quality
* Songs:
    * AAC 256kbps M4A
* Music Videos (varies depending on the video):
    * HEVC 4K 12~20mbps M4V / AAC 256kbps (achieved by using `--prefer-hevc` argument)
    * AVC 1080p 6.5~10mbps M4V / AAC 256kbps
    * AVC 720p 4mbps M4V / AAC 256kbps
    * AVC 480p 1.5mbps M4V / AAC 256kbps
    * AVC 360p 1mbps M4V / AAC 256kbps
