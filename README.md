# Glomatico's ✨ Apple Music ✨ Downloader
A Python script to download Apple Music songs/music videos/albums/playlists.

This is a rework of https://github.com/Slyyxp/AppleMusic-Downloader/tree/a6e18de8da4694219924affaa2b5686930e39e84.

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
        * MP4Box (Linux): https://drive.google.com/open?id=1rgEkkmsDnzF3SECEpyxz3m-EuJzUj182
        * mp4decrypt (Linux): https://drive.google.com/open?id=16qZfStVibIGgH3xSKmAE1Wuf17DTDR8q
4. Create a folder called `login` on the same folder as the script and put your `cookies.txt` and `token.txt` files there
    * You can get your cookies by using this Google Chrome extension on Apple Music website: https://chrome.google.com/webstore/detail/cookies-txt/njabckikapfpffapmjgojcnbfjonfjfg. Make sure to export it as `cookies.txt` and put it on the `login` folder as described above.
    * You can get your token by looking at the network requests on Apple Music website. 
        * On Google Chrome, you can do this by pressing F12 on Apple Music website and then clicking on the `Network` tab. Then, start navigating throught Apple Music website, filter the requests by `amp-api` and click on one that has `authorization` on the `Request Headers` section. Copy the value of the `authorization` header, paste it on a text file and save it as `token.txt` on the `login` folder.
        ![](https://i.imgur.com/9YyfGn4.png)
    * If you have previously used the old version of this script, you can just copy your `cookies.txt` and `token.txt` files from the old version to the `login` folder. You will have to add `Bearer ` before your token on the `token.txt` file.
5. Get your L3 CDM (`device_client_id_blob` and `device_private_key` files) and put them on `pywidevine/L3/cdm/devices` folder
    * You can get your L3 CDM by using wvdumper: https://github.com/wvdumper/dumper
## Usage
```
python gamdl.py [OPTIONS] [URLS]
```
Use `--help` argument to see all available options.
