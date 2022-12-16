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
    * Or you can use the ones I provided and add them to your PATH or put them on the same folder as the script:
        * MP4Box (Windows): https://drive.google.com/open?id=1o202Kese9Q4dKzzgEtZnznuUx_eJp0bH
        * mp4decrypt (Windows): https://drive.google.com/open?id=1K6Y98zSfHowALq4FJ6MYrhg1oFBltci9
        * MP4Box (Linux): TBD
        * mp4decrypt (Linux): https://drive.google.com/open?id=16qZfStVibIGgH3xSKmAE1Wuf17DTDR8q
4. Export your Apple Music cookies as `cookies.txt` and put it in the same folder as the script
    * You can export your cookies by using this Google Chrome extension on Apple Music website: https://chrome.google.com/webstore/detail/cookies-txt/njabckikapfpffapmjgojcnbfjonfjfg. Make sure to be logged in.
5. Put your L3 CDM (`device_client_id_blob` and `device_private_key` files) on `pywidevine/L3/cdm/devices/android_generic` folder
    * You can get your L3 CDM by using wvdumper: https://github.com/wvdumper/dumper

## Usage
```
python gamdl.py [OPTIONS] [URLS]
```
Tracks are saved in `./Apple Music` by default, but the directory can be changed using `--final-path` argument.

Use `--help` argument to see all available options.
