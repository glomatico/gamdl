import base64
import datetime
import functools
import re
import shutil
import subprocess
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from xml.etree import ElementTree

import m3u8
import requests
from mutagen.mp4 import MP4, MP4Cover
from pywidevine import PSSH, Cdm, Device, WidevinePsshData
from yt_dlp import YoutubeDL

import gamdl.storefronts

MP4_TAGS_MAP = {
    "album": "\xa9alb",
    "album_artist": "aART",
    "album_id": "plID",
    "album_sort": "soal",
    "artist": "\xa9ART",
    "artist_id": "atID",
    "artist_sort": "soar",
    "comment": "\xa9cmt",
    "composer": "\xa9wrt",
    "composer_id": "cmID",
    "composer_sort": "soco",
    "copyright": "cprt",
    "genre": "\xa9gen",
    "genre_id": "geID",
    "lyrics": "\xa9lyr",
    "media_type": "stik",
    "release_date": "\xa9day",
    "storefront": "sfID",
    "title": "\xa9nam",
    "title_id": "cnID",
    "title_sort": "sonm",
    "xid": "xid ",
}


class Dl:
    def __init__(
        self,
        final_path: Path = None,
        temp_path: Path = None,
        cookies_location: Path = None,
        wvd_location: Path = None,
        ffmpeg_location: str = None,
        mp4box_location: str = None,
        mp4decrypt_location: str = None,
        nm3u8dlre_location: str = None,
        template_folder_album: str = None,
        template_folder_compilation: str = None,
        template_file_single_disc: str = None,
        template_file_multi_disc: str = None,
        template_folder_music_video: str = None,
        template_file_music_video: str = None,
        cover_size: int = None,
        cover_format: str = None,
        remux_mode: str = None,
        download_mode: str = None,
        exclude_tags: str = None,
        truncate: int = None,
        prefer_hevc: bool = None,
        ask_video_format: bool = None,
        disable_music_video_album_skip: bool = None,
        lrc_only: bool = None,
        songs_heaac: bool = None,
        **kwargs,
    ):
        self.final_path = final_path
        self.temp_path = temp_path
        self.ffmpeg_location = shutil.which(ffmpeg_location)
        self.mp4box_location = shutil.which(mp4box_location)
        self.mp4decrypt_location = shutil.which(mp4decrypt_location)
        self.nm3u8dlre_location = shutil.which(nm3u8dlre_location)
        self.template_folder_album = template_folder_album
        self.template_folder_compilation = template_folder_compilation
        self.template_file_single_disc = template_file_single_disc
        self.template_file_multi_disc = template_file_multi_disc
        self.template_folder_music_video = template_folder_music_video
        self.template_file_music_video = template_file_music_video
        self.cover_size = cover_size
        self.cover_format = cover_format
        self.remux_mode = remux_mode
        self.download_mode = download_mode
        self.exclude_tags = (
            [i.lower() for i in exclude_tags.split(",")]
            if exclude_tags is not None
            else []
        )
        self.truncate = None if truncate is not None and truncate < 4 else truncate
        self.prefer_hevc = prefer_hevc
        self.ask_video_format = ask_video_format
        self.disable_music_video_album_skip = disable_music_video_album_skip
        self.songs_flavor = "32:ctrp64" if songs_heaac else "28:ctrp256"
        if not lrc_only:
            self.cdm = Cdm.from_device(Device.load(wvd_location))
            self.cdm_session = self.cdm.open()
        cookies = MozillaCookieJar(cookies_location)
        cookies.load(ignore_discard=True, ignore_expires=True)
        self.session = requests.Session()
        self.session.cookies.update(cookies)
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:95.0) Gecko/20100101 Firefox/95.0",
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate, br",
                "content-type": "application/json",
                "Media-User-Token": self.session.cookies.get_dict()["media-user-token"],
                "x-apple-renewal": "true",
                "DNT": "1",
                "Connection": "keep-alive",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
                "origin": "https://beta.music.apple.com",
            }
        )
        home_page = self.session.get("https://beta.music.apple.com").text
        index_js_uri = re.search(r"/(assets/index-legacy-[^/]+\.js)", home_page).group(
            1
        )
        index_js_page = self.session.get(
            f"https://beta.music.apple.com/{index_js_uri}"
        ).text
        token = re.search('(?=eyJh)(.*?)(?=")', index_js_page).group(1)
        self.session.headers.update({"authorization": f"Bearer {token}"})
        self.country = self.session.cookies.get_dict()["itua"]
        self.storefront = getattr(gamdl.storefronts, self.country.upper())

    def get_download_queue(self, url):
        download_queue = []
        track_id = url.split("/")[-1].split("i=")[-1].split("&")[0].split("?")[0]
        response = self.session.get(
            f"https://amp-api.music.apple.com/v1/catalog/{self.country}",
            params={
                "ids[songs]": track_id,
                "ids[albums]": track_id,
                "ids[playlists]": track_id,
                "ids[music-videos]": track_id,
            },
        ).json()["data"][0]
        if response["type"] == "songs" and "playParams" in response["attributes"]:
            download_queue.append(response)
        if (
            response["type"] == "music-videos"
            and "playParams" in response["attributes"]
        ):
            download_queue.append(response)
        if response["type"] in ("albums", "playlists"):
            for track in response["relationships"]["tracks"]["data"]:
                if "playParams" in track["attributes"]:
                    if (
                        track["type"] == "music-videos"
                        and self.disable_music_video_album_skip
                    ):
                        download_queue.append(track)
                    if track["type"] == "songs":
                        download_queue.append(track)
        if not download_queue:
            raise Exception("Criteria not met")
        return download_queue

    def get_webplayback(self, track_id):
        response = self.session.post(
            "https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/webPlayback",
            json={
                "salableAdamId": track_id,
                "language": "en-US",
            },
        ).json()["songList"][0]
        return response

    def get_stream_url_song(self, webplayback):
        return next(
            i for i in webplayback["assets"] if i["flavor"] == self.songs_flavor
        )["URL"]

    def get_stream_url_music_video(self, webplayback):
        ydl = YoutubeDL(
            {
                "allow_unplayable_formats": True,
                "quiet": True,
                "no_warnings": True,
            }
        )
        playlist = ydl.extract_info(
            webplayback["hls-playlist-url"].replace("&aec=HD", ""),
            download=False,
        )
        if self.ask_video_format:
            ydl.list_formats(playlist)
            while True:
                format_ids = input("Enter video and audio id: ").split()
                if len(format_ids) != 2:
                    continue
                video_id, audio_id = format_ids
                matching_formats = [
                    i
                    for i in playlist["formats"]
                    if i["format_id"] in (video_id, audio_id)
                ]
                stream_url_video = next(
                    (i["url"] for i in matching_formats if i["video_ext"] != "none"),
                    None,
                )
                stream_url_audio = next(
                    (i["url"] for i in matching_formats if i["audio_ext"] != "none"),
                    None,
                )
                if stream_url_video is not None and stream_url_audio is not None:
                    break
        else:
            if self.prefer_hevc:
                stream_url_video = playlist["formats"][-1]["url"]
            else:
                stream_url_video = list(
                    i["url"]
                    for i in playlist["formats"]
                    if i["video_ext"] != "none" and "avc1" in i["vcodec"]
                )[-1]
            stream_url_audio = next(
                i["url"]
                for i in playlist["formats"]
                if "audio-stereo-256" in i["format_id"]
            )
        return stream_url_video, stream_url_audio

    def get_encrypted_location_video(self, track_id):
        return self.temp_path / f"{track_id}_encrypted_video.mp4"

    def get_encrypted_location_audio(self, track_id):
        return self.temp_path / f"{track_id}_encrypted_audio.m4a"

    def get_decrypted_location_video(self, track_id):
        return self.temp_path / f"{track_id}_decrypted_video.mp4"

    def get_decrypted_location_audio(self, track_id):
        return self.temp_path / f"{track_id}_decrypted_audio.m4a"

    def get_fixed_location(self, track_id, file_extension):
        return self.temp_path / f"{track_id}_fixed{file_extension}"

    def get_cover_location_song(self, final_location):
        return final_location.parent / f"Cover.{self.cover_format}"

    def get_cover_location_music_video(self, final_location):
        return final_location.with_suffix(f".{self.cover_format}")

    def get_lrc_location(self, final_location):
        return final_location.with_suffix(".lrc")

    def download(self, encrypted_location, stream_url):
        if self.download_mode == "yt-dlp":
            params = {
                "quiet": True,
                "no_warnings": True,
                "outtmpl": str(encrypted_location),
                "allow_unplayable_formats": True,
                "fixup": "never",
            }
            with YoutubeDL(params) as ydl:
                ydl.download(stream_url)
        else:
            subprocess.run(
                [
                    self.nm3u8dlre_location,
                    stream_url,
                    "--binary-merge",
                    "--no-log",
                    "--log-level",
                    "off",
                    "--ffmpeg-binary-path",
                    self.ffmpeg_location,
                    "--save-name",
                    encrypted_location.stem,
                    "--save-dir",
                    encrypted_location.parent,
                    "--tmp-dir",
                    encrypted_location.parent,
                ],
                check=True,
            )

    def get_license_b64(self, challenge, track_uri, track_id):
        return self.session.post(
            "https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/acquireWebPlaybackLicense",
            json={
                "challenge": challenge,
                "key-system": "com.widevine.alpha",
                "uri": track_uri,
                "adamId": track_id,
                "isLibrary": False,
                "user-initiated": True,
            },
        ).json()["license"]

    def get_decryption_key_music_video(self, stream_url, track_id):
        playlist = m3u8.load(stream_url)
        track_uri = next(
            i
            for i in playlist.keys
            if i.keyformat == "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
        ).uri
        pssh = PSSH(track_uri.split(",")[1])
        challenge = base64.b64encode(
            self.cdm.get_license_challenge(self.cdm_session, pssh)
        ).decode()
        license_b64 = self.get_license_b64(challenge, track_uri, track_id)
        self.cdm.parse_license(self.cdm_session, license_b64)
        return next(
            i for i in self.cdm.get_keys(self.cdm_session) if i.type == "CONTENT"
        ).key.hex()

    def get_decryption_key_song(self, stream_url, track_id):
        track_uri = m3u8.load(stream_url).keys[0].uri
        widevine_pssh_data = WidevinePsshData()
        widevine_pssh_data.algorithm = 1
        widevine_pssh_data.key_ids.append(base64.b64decode(track_uri.split(",")[1]))
        pssh = PSSH(base64.b64encode(widevine_pssh_data.SerializeToString()).decode())
        challenge = base64.b64encode(
            self.cdm.get_license_challenge(self.cdm_session, pssh)
        ).decode()
        license_b64 = self.get_license_b64(challenge, track_uri, track_id)
        self.cdm.parse_license(self.cdm_session, license_b64)
        return next(
            i for i in self.cdm.get_keys(self.cdm_session) if i.type == "CONTENT"
        ).key.hex()

    def get_synced_lyrics_lrc_timestamp(self, ttml_timestamp):
        mins = int(ttml_timestamp.split(":")[-2]) if ":" in ttml_timestamp else 0
        secs, ms = str(
            float(ttml_timestamp.split(":")[-1])
            if ":" in ttml_timestamp
            else float(ttml_timestamp.replace("s", ""))
        ).split(".")
        secs = int(secs)
        ms = int(ms)
        lrc_timestamp = datetime.datetime.fromtimestamp(
            (mins * 60) + secs + (ms / 1000)
        )
        ms_new = lrc_timestamp.strftime("%f")[:-3]
        if int(ms_new[-1]) >= 5:
            ms = int(f"{int(ms_new[:2]) + 1}") * 10
            lrc_timestamp += datetime.timedelta(milliseconds=ms) - datetime.timedelta(
                microseconds=lrc_timestamp.microsecond
            )
        return lrc_timestamp.strftime("%M:%S.%f")[:-4]

    def get_lyrics(self, track_id):
        try:
            lyrics_ttml = ElementTree.fromstring(
                self.session.get(
                    f"https://amp-api.music.apple.com/v1/catalog/{self.country}/songs/{track_id}/lyrics"
                ).json()["data"][0]["attributes"]["ttml"]
            )
        except:
            return None, None
        unsynced_lyrics = ""
        synced_lyrics = ""
        for div in lyrics_ttml.iter("{http://www.w3.org/ns/ttml}div"):
            for p in div.iter("{http://www.w3.org/ns/ttml}p"):
                if p.attrib.get("begin"):
                    synced_lyrics += f'[{self.get_synced_lyrics_lrc_timestamp(p.attrib.get("begin"))}]{p.text}\n'
                if p.text is not None:
                    unsynced_lyrics += p.text + "\n"
            unsynced_lyrics += "\n"
        return unsynced_lyrics[:-2], synced_lyrics

    @functools.lru_cache()
    def get_cover(self, cover_url):
        return requests.get(cover_url).content

    def get_tags_song(self, webplayback, unsynced_lyrics):
        flavor = next(
            i for i in webplayback["assets"] if i["flavor"] == self.songs_flavor
        )
        metadata = flavor["metadata"]
        cover_url = flavor["artworkURL"].replace(
            "600x600bb.jpg",
            f"{self.cover_size}x{self.cover_size}bb.{self.cover_format}",
        )
        tags = {
            "album": metadata["playlistName"],
            "album_artist": metadata["playlistArtistName"],
            "album_id": int(metadata["playlistId"]),
            "album_sort": metadata["sort-album"],
            "artist": metadata["artistName"],
            "artist_id": int(metadata["artistId"]),
            "artist_sort": metadata["sort-artist"],
            "compilation": metadata["compilation"],
            "cover_url": cover_url,
            "disc": metadata["discNumber"],
            "disc_total": metadata["discCount"],
            "gapless": metadata["gapless"],
            "genre": metadata["genre"],
            "genre_id": metadata["genreId"],
            "media_type": 1,
            "rating": metadata["explicit"],
            "storefront": metadata["s"],
            "title": metadata["itemName"],
            "title_id": int(metadata["itemId"]),
            "title_sort": metadata["sort-name"],
            "track": metadata["trackNumber"],
            "track_total": metadata["trackCount"],
        }
        if "comments" in metadata:
            tags["comment"] = metadata["comments"]
        if "composerId" in metadata:
            tags["composer"] = metadata["composerName"]
            tags["composer_id"] = int(metadata["composerId"])
            tags["composer_sort"] = metadata["sort-composer"]
        if "copyright" in metadata:
            tags["copyright"] = metadata["copyright"]
        if "releaseDate" in metadata:
            tags["release_date"] = metadata["releaseDate"]
        if "xid" in metadata:
            tags["xid"] = metadata["xid"]
        if unsynced_lyrics:
            tags["lyrics"] = unsynced_lyrics
        return tags

    def get_tags_music_video(self, track_id):
        metadata = requests.get(
            f"https://itunes.apple.com/lookup",
            params={
                "id": track_id,
                "entity": "album",
                "country": self.country,
                "lang": "en_US",
            },
        ).json()["results"]
        extra_metadata = requests.get(
            f'https://music.apple.com/music-video/{metadata[0]["trackId"]}',
            headers={"X-Apple-Store-Front": f"{self.storefront} t:music31"},
        ).json()["storePlatformData"]["product-dv"]["results"][
            str(metadata[0]["trackId"])
        ]
        tags = {
            "artist": metadata[0]["artistName"],
            "artist_id": metadata[0]["artistId"],
            "cover_url": metadata[0]["artworkUrl30"].replace(
                "30x30bb.jpg",
                f"{self.cover_size}x{self.cover_size}bb.{self.cover_format}",
            ),
            "genre": metadata[0]["primaryGenreName"],
            "genre_id": int(extra_metadata["genres"][0]["genreId"]),
            "media_type": 6,
            "release_date": metadata[0]["releaseDate"],
            "storefront": int(self.storefront.split("-")[0]),
            "title": metadata[0]["trackCensoredName"],
            "title_id": metadata[0]["trackId"],
        }
        if "copyright" in extra_metadata:
            tags["copyright"] = extra_metadata["copyright"]
        if metadata[0]["trackExplicitness"] == "notExplicit":
            tags["rating"] = 0
        elif metadata[0]["trackExplicitness"] == "explicit":
            tags["rating"] = 1
        else:
            tags["rating"] = 2
        if len(metadata) > 1:
            tags["album"] = metadata[1]["collectionCensoredName"]
            tags["album_artist"] = metadata[1]["artistName"]
            tags["album_id"] = metadata[1]["collectionId"]
            tags["disc"] = metadata[0]["discNumber"]
            tags["disc_total"] = metadata[0]["discCount"]
            tags["track"] = metadata[0]["trackNumber"]
            tags["track_total"] = metadata[0]["trackCount"]
        return tags

    def get_sanitized_string(self, dirty_string, is_folder):
        dirty_string = re.sub(r'[\\/:*?"<>|;]', "_", dirty_string)
        if is_folder:
            dirty_string = dirty_string[: self.truncate]
            if dirty_string.endswith("."):
                dirty_string = dirty_string[:-1] + "_"
        else:
            if self.truncate is not None:
                dirty_string = dirty_string[: self.truncate - 4]
        return dirty_string.strip()

    def get_final_location(self, tags):
        if "album" in tags:
            final_location_folder = (
                self.template_folder_compilation.split("/")
                if "compilation" in tags and tags["compilation"]
                else self.template_folder_album.split("/")
            )
            final_location_file = (
                self.template_file_multi_disc.split("/")
                if tags["disc_total"] > 1
                else self.template_file_single_disc.split("/")
            )
        else:
            final_location_folder = self.template_folder_music_video.split("/")
            final_location_file = self.template_file_music_video.split("/")
        file_extension = ".m4a" if tags["media_type"] == 1 else ".m4v"
        final_location_folder = [
            self.get_sanitized_string(i.format(**tags), True)
            for i in final_location_folder
        ]
        final_location_file = [
            self.get_sanitized_string(i.format(**tags), True)
            for i in final_location_file[:-1]
        ] + [
            self.get_sanitized_string(final_location_file[-1].format(**tags), False)
            + file_extension
        ]
        return self.final_path.joinpath(*final_location_folder).joinpath(
            *final_location_file
        )

    def decrypt(self, encrypted_location, decrypted_location, decryption_key):
        subprocess.run(
            [
                self.mp4decrypt_location,
                encrypted_location,
                "--key",
                f"1:{decryption_key}",
                decrypted_location,
            ],
        )

    def fixup_song_mp4box(self, decrypted_location, fixed_location):
        subprocess.run(
            [
                self.mp4box_location,
                "-quiet",
                "-add",
                decrypted_location,
                "-itags",
                "artist=placeholder",
                "-new",
                fixed_location,
            ],
        )

    def fixup_music_video_mp4box(
        self, decrypted_location_audio, decrypted_location_video, fixed_location
    ):
        subprocess.run(
            [
                self.mp4box_location,
                "-quiet",
                "-add",
                decrypted_location_audio,
                "-add",
                decrypted_location_video,
                "-itags",
                "artist=placeholder",
                "-new",
                fixed_location,
            ],
            check=True,
        )

    def fixup_song_ffmpeg(self, encrypted_location, decryption_key, fixed_location):
        subprocess.run(
            [
                self.ffmpeg_location,
                "-loglevel",
                "error",
                "-y",
                "-decryption_key",
                decryption_key,
                "-i",
                encrypted_location,
                "-movflags",
                "+faststart",
                "-c",
                "copy",
                fixed_location,
            ],
            check=True,
        )

    def fixup_music_video_ffmpeg(
        self, decrypted_location_video, decrypted_location_audio, fixed_location
    ):
        subprocess.run(
            [
                self.ffmpeg_location,
                "-loglevel",
                "error",
                "-y",
                "-i",
                decrypted_location_video,
                "-i",
                decrypted_location_audio,
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                "-c",
                "copy",
                fixed_location,
            ],
            check=True,
        )

    def apply_tags(self, fixed_location, tags):
        mp4_tags = {
            v: [tags[k]]
            for k, v in MP4_TAGS_MAP.items()
            if k not in self.exclude_tags and tags.get(k) is not None
        }
        if not {"disc", "disc_total"} & set(self.exclude_tags) and "disc" in tags:
            mp4_tags["disk"] = [[0, 0]]
        if not {"track", "track_total"} & set(self.exclude_tags) and "track" in tags:
            mp4_tags["trkn"] = [[0, 0]]
        if "compilation" not in self.exclude_tags and "compilation" in tags:
            mp4_tags["cpil"] = tags["compilation"]
        if "cover" not in self.exclude_tags:
            mp4_tags["covr"] = [
                MP4Cover(
                    self.get_cover(tags["cover_url"]),
                    imageformat=MP4Cover.FORMAT_JPEG
                    if self.cover_format == "jpg"
                    else MP4Cover.FORMAT_PNG,
                )
            ]
        if "disc" not in self.exclude_tags and "disc" in tags:
            mp4_tags["disk"][0][0] = tags["disc"]
        if "disc_total" not in self.exclude_tags and "disc_total" in tags:
            mp4_tags["disk"][0][1] = tags["disc_total"]
        if "gapless" not in self.exclude_tags and "gapless" in tags:
            mp4_tags["pgap"] = tags["gapless"]
        if "track" not in self.exclude_tags and "track" in tags:
            mp4_tags["trkn"][0][0] = tags["track"]
        if "track_total" not in self.exclude_tags and "track_total" in tags:
            mp4_tags["trkn"][0][1] = tags["track_total"]
        mp4 = MP4(fixed_location)
        mp4.clear()
        mp4.update(mp4_tags)
        mp4.save()

    def move_to_final_location(self, fixed_location, final_location):
        final_location.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(fixed_location, final_location)

    def save_cover(self, tags, cover_location):
        with open(cover_location, "wb") as f:
            f.write(self.get_cover(tags["cover_url"]))

    def make_lrc(self, lrc_location, synced_lyrics):
        lrc_location.parent.mkdir(parents=True, exist_ok=True)
        with open(lrc_location, "w", encoding="utf8") as f:
            f.write(synced_lyrics)
