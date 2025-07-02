from __future__ import annotations

import base64
import datetime
import json
import re
import subprocess
from pathlib import Path
from xml.dom import minidom
from xml.etree import ElementTree

import m3u8
from InquirerPy import inquirer
from InquirerPy.base.control import Choice

from .constants import SONG_CODEC_REGEX_MAP, SYNCED_LYRICS_FILE_EXTENSION_MAP
from .downloader import Downloader
from .enums import MediaFileFormat, RemuxMode, SongCodec, SyncedLyricsFormat
from .models import Lyrics, StreamInfo, StreamInfoAv


class DownloaderSong:
    DEFAULT_DECRYPTION_KEY = "32b8ade1769e26b1ffb8986352793fc6"
    MP4_FORMAT_CODECS = ["ec-3"]

    def __init__(
        self,
        downloader: Downloader,
        codec: SongCodec = SongCodec.AAC_LEGACY,
        synced_lyrics_format: SyncedLyricsFormat = SyncedLyricsFormat.LRC,
    ):
        self.downloader = downloader
        self.codec = codec
        self.synced_lyrics_format = synced_lyrics_format

    def get_drm_infos(self, m3u8_data: dict) -> dict:
        drm_info_raw = next(
            (
                session_data
                for session_data in m3u8_data["session_data"]
                if session_data["data_id"] == "com.apple.hls.AudioSessionKeyInfo"
            ),
            None,
        )
        if not drm_info_raw:
            return None
        return json.loads(base64.b64decode(drm_info_raw["value"]).decode("utf-8"))

    def get_asset_infos(self, m3u8_data: dict) -> dict:
        return json.loads(
            base64.b64decode(
                next(
                    session_data
                    for session_data in m3u8_data["session_data"]
                    if session_data["data_id"] == "com.apple.hls.audioAssetMetadata"
                )["value"]
            ).decode("utf-8")
        )

    def get_playlist_from_codec(self, m3u8_data: dict) -> dict | None:
        m3u8_master_playlists = [
            playlist
            for playlist in m3u8_data["playlists"]
            if re.fullmatch(
                SONG_CODEC_REGEX_MAP[self.codec], playlist["stream_info"]["audio"]
            )
        ]
        if not m3u8_master_playlists:
            return None
        m3u8_master_playlists.sort(key=lambda x: x["stream_info"]["average_bandwidth"])
        return m3u8_master_playlists[-1]

    def get_playlist_from_user(self, m3u8_data: dict) -> dict | None:
        m3u8_master_playlists = [playlist for playlist in m3u8_data["playlists"]]
        choices = [
            Choice(
                name=playlist["stream_info"]["audio"],
                value=playlist,
            )
            for playlist in m3u8_master_playlists
        ]
        selected = inquirer.select(
            message="Select which codec to download:",
            choices=choices,
        ).execute()
        return selected

    def _get_drm_data(
        self,
        drm_infos: dict,
        drm_ids: list,
        drm_key: str,
    ) -> str | None:
        drm_info = next(
            (
                drm_infos[drm_id]
                for drm_id in drm_ids
                if drm_infos[drm_id].get(drm_key) and drm_id != "1"
            ),
            None,
        )
        if not drm_info:
            return None
        return drm_info[drm_key]["URI"]

    def get_widevine_pssh(
        self,
        drm_infos: dict,
        drm_ids: list,
    ) -> str | None:
        return self._get_drm_data(
            drm_infos,
            drm_ids,
            "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",
        )

    def get_playready_pssh(self, drm_infos: dict, drm_ids: list) -> str | None:
        return self._get_drm_data(
            drm_infos,
            drm_ids,
            "com.microsoft.playready",
        )

    def get_fairplay_key(self, drm_infos: dict, drm_ids: list) -> str | None:
        return self._get_drm_data(
            drm_infos,
            drm_ids,
            "com.apple.streamingkeydelivery",
        )

    def get_stream_info(self, track_metadata: dict) -> StreamInfoAv | None:
        m3u8_url = track_metadata["attributes"]["extendedAssetUrls"].get("enhancedHls")
        if not m3u8_url:
            return None
        return self._get_stream_info(m3u8_url)

    def _get_stream_info(self, m3u8_url: str) -> StreamInfoAv | None:
        stream_info = StreamInfo()
        m3u8_obj = m3u8.load(m3u8_url)
        m3u8_data = m3u8_obj.data
        drm_infos = self.get_drm_infos(m3u8_data)
        if not drm_infos:
            return None
        asset_infos = self.get_asset_infos(m3u8_data)
        if self.codec == SongCodec.ASK:
            playlist = self.get_playlist_from_user(m3u8_data)
        else:
            playlist = self.get_playlist_from_codec(m3u8_data)
        if playlist is None:
            return None
        stream_info.stream_url = m3u8_obj.base_uri + playlist["uri"]
        variant_id = playlist["stream_info"]["stable_variant_id"]
        drm_ids = asset_infos[variant_id]["AUDIO-SESSION-KEY-IDS"]
        widevine_pssh, playready_pssh, fairplay_key = (
            self.get_widevine_pssh(drm_infos, drm_ids),
            self.get_playready_pssh(drm_infos, drm_ids),
            self.get_fairplay_key(drm_infos, drm_ids),
        )
        stream_info.widevine_pssh = widevine_pssh
        stream_info.playready_pssh = playready_pssh
        stream_info.fairplay_key = fairplay_key
        stream_info.codec = playlist["stream_info"]["codecs"]
        is_mp4 = any(
            stream_info.codec.startswith(possible_codec)
            for possible_codec in self.MP4_FORMAT_CODECS
        )
        return StreamInfoAv(
            audio_track=stream_info,
            file_format=MediaFileFormat.MP4 if is_mp4 else MediaFileFormat.M4A,
        )

    @staticmethod
    def parse_datetime_obj_from_timestamp_ttml(
        timestamp_ttml: str,
    ) -> datetime.datetime:
        mins_secs_ms = re.findall(r"\d+", timestamp_ttml)
        ms, secs, mins = 0, 0, 0
        if len(mins_secs_ms) == 2 and ":" in timestamp_ttml:
            secs, mins = int(mins_secs_ms[-1]), int(mins_secs_ms[-2])
        elif len(mins_secs_ms) == 1:
            ms = int(mins_secs_ms[-1])
        else:
            secs = float(f"{mins_secs_ms[-2]}.{mins_secs_ms[-1]}")
            if len(mins_secs_ms) > 2:
                mins = int(mins_secs_ms[-3])
        return datetime.datetime.fromtimestamp(
            (mins * 60) + secs + (ms / 1000),
            tz=datetime.timezone.utc,
        )

    def get_lyrics_synced_timestamp_lrc(self, timestamp_ttml: str) -> str:
        datetime_obj = self.parse_datetime_obj_from_timestamp_ttml(timestamp_ttml)
        ms_new = datetime_obj.strftime("%f")[:-3]
        if int(ms_new[-1]) >= 5:
            ms = int(f"{int(ms_new[:2]) + 1}") * 10
            datetime_obj += datetime.timedelta(milliseconds=ms) - datetime.timedelta(
                microseconds=datetime_obj.microsecond
            )
        return datetime_obj.strftime("%M:%S.%f")[:-4]

    def get_lyrics_synced_timestamp_srt(self, timestamp_ttml: str) -> str:
        datetime_obj = self.parse_datetime_obj_from_timestamp_ttml(timestamp_ttml)
        return datetime_obj.strftime("00:%M:%S,%f")[:-3]

    def get_lyrics_synced_line_lrc(self, timestamp_ttml: str, text: str) -> str:
        return f"[{self.get_lyrics_synced_timestamp_lrc(timestamp_ttml)}]{text}"

    def get_lyrics_synced_line_srt(
        self,
        index: int,
        timestamp_ttml_start: str,
        timestamp_ttml_end: str,
        text: str,
    ) -> str:
        timestamp_srt_start = self.get_lyrics_synced_timestamp_srt(timestamp_ttml_start)
        timestamp_srt_end = self.get_lyrics_synced_timestamp_srt(timestamp_ttml_end)
        return f"{index}\n{timestamp_srt_start} --> {timestamp_srt_end}\n{text}\n"

    def get_lyrics(self, track_metadata: dict) -> Lyrics | None:
        lyrics = Lyrics()
        if not track_metadata["attributes"]["hasLyrics"]:
            return None
        elif track_metadata.get("relationships") is None:
            track_metadata = self.downloader.apple_music_api.get_song(
                self.downloader.get_media_id(track_metadata)
            )
        if (
            track_metadata["relationships"].get("lyrics")
            and track_metadata["relationships"]["lyrics"].get("data")
            and track_metadata["relationships"]["lyrics"]["data"][0].get("attributes")
        ):
            lyrics = self._get_lyrics(
                track_metadata["relationships"]["lyrics"]["data"][0]["attributes"][
                    "ttml"
                ]
            )
        return lyrics

    def _get_lyrics(self, lyrics_ttml: str) -> Lyrics:
        lyrics = Lyrics("", "")
        lyrics_ttml_et = ElementTree.fromstring(lyrics_ttml)
        index = 1
        for div in lyrics_ttml_et.iter("{http://www.w3.org/ns/ttml}div"):
            for p in div.iter("{http://www.w3.org/ns/ttml}p"):
                if p.text is not None:
                    lyrics.unsynced += p.text + "\n"
                if p.attrib.get("begin"):
                    if self.synced_lyrics_format == SyncedLyricsFormat.LRC:
                        lyrics.synced += f"{self.get_lyrics_synced_line_lrc(p.attrib.get('begin'), p.text)}"
                    elif self.synced_lyrics_format == SyncedLyricsFormat.SRT:
                        lyrics.synced += f"{self.get_lyrics_synced_line_srt(index, p.attrib.get('begin'), p.attrib.get('end'), p.text)}"
                    elif self.synced_lyrics_format == SyncedLyricsFormat.TTML:
                        if not lyrics.synced:
                            lyrics.synced = minidom.parseString(
                                lyrics_ttml
                            ).toprettyxml()
                        continue
                    lyrics.synced += "\n"
                    index += 1
            lyrics.unsynced += "\n"
        lyrics.unsynced = lyrics.unsynced[:-2]
        return lyrics

    def get_tags(self, webplayback: dict, lyrics_unsynced: str) -> dict:
        tags_raw = webplayback["assets"][0]["metadata"]
        tags = {
            "album": tags_raw["playlistName"],
            "album_artist": tags_raw["playlistArtistName"],
            "album_id": int(tags_raw["playlistId"]),
            "album_sort": tags_raw["sort-album"],
            "artist": tags_raw["artistName"],
            "artist_id": int(tags_raw["artistId"]),
            "artist_sort": tags_raw["sort-artist"],
            "comments": tags_raw.get("comments"),
            "compilation": tags_raw["compilation"],
            "composer": tags_raw.get("composerName"),
            "composer_id": (
                int(tags_raw.get("composerId")) if tags_raw.get("composerId") else None
            ),
            "composer_sort": tags_raw.get("sort-composer"),
            "copyright": tags_raw.get("copyright"),
            "date": (
                self.downloader.sanitize_date(tags_raw["releaseDate"])
                if tags_raw.get("releaseDate")
                else None
            ),
            "disc": tags_raw["discNumber"],
            "disc_total": tags_raw["discCount"],
            "gapless": tags_raw["gapless"],
            "genre": tags_raw.get("genre"),
            "genre_id": tags_raw["genreId"],
            "lyrics": lyrics_unsynced if lyrics_unsynced else None,
            "media_type": 1,
            "rating": tags_raw["explicit"],
            "storefront": tags_raw["s"],
            "title": tags_raw["itemName"],
            "title_id": int(tags_raw["itemId"]),
            "title_sort": tags_raw["sort-name"],
            "track": tags_raw["trackNumber"],
            "track_total": tags_raw["trackCount"],
            "xid": tags_raw.get("xid"),
        }
        return tags

    def get_encrypted_path(self, track_id: str) -> Path:
        return self.downloader.temp_path / f"{track_id}_encrypted.m4a"

    def get_decrypted_path(self, track_id: str) -> Path:
        return self.downloader.temp_path / f"{track_id}_decrypted.m4a"

    def get_remuxed_path(self, track_id: str, file_format: MediaFileFormat) -> Path:
        return (
            self.downloader.temp_path
            / f"{track_id}_remuxed.{"m4a" if file_format == MediaFileFormat.M4A else "mp4"}"
        )

    def fix_key_id(self, encrypted_path: Path):
        count = 0
        with open(encrypted_path, "rb+") as file:
            while data := file.read(4096):
                pos = file.tell()
                i = 0
                while tenc := max(0, data.find(b"tenc", i)):
                    kid = tenc + 12
                    file.seek(max(0, pos - 4096) + kid, 0)
                    file.write(bytes.fromhex(f"{count:032}"))
                    count += 1
                    i = kid + 1
                file.seek(pos, 0)

    def decrypt(
        self,
        encrypted_path: Path,
        decrypted_path: Path,
        decryption_key: str,
    ):
        self.fix_key_id(encrypted_path)
        subprocess.run(
            [
                self.downloader.mp4decrypt_path_full,
                encrypted_path,
                "--key",
                f"00000000000000000000000000000001:{decryption_key}",
                "--key",
                f"00000000000000000000000000000000:{self.DEFAULT_DECRYPTION_KEY}",
                decrypted_path,
            ],
            check=True,
            **self.downloader.subprocess_additional_args,
        )

    def remux(self, decrypted_path: Path, remuxed_path: Path):
        if self.downloader.remux_mode == RemuxMode.MP4BOX:
            self.remux_mp4box(decrypted_path, remuxed_path)
        elif self.downloader.remux_mode == RemuxMode.FFMPEG:
            self.remux_ffmpeg(decrypted_path, remuxed_path)

    def remux_mp4box(self, decrypted_path: Path, remuxed_path: Path):
        subprocess.run(
            [
                self.downloader.mp4box_path_full,
                "-quiet",
                "-add",
                decrypted_path,
                "-itags",
                "artist=placeholder",
                "-keep-utc",
                "-new",
                remuxed_path,
            ],
            check=True,
            **self.downloader.subprocess_additional_args,
        )

    def remux_ffmpeg(
        self,
        decrypted_path: Path,
        remuxed_path: Path,
    ):
        subprocess.run(
            [
                self.downloader.ffmpeg_path_full,
                "-loglevel",
                "error",
                "-y",
                "-i",
                decrypted_path,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                remuxed_path,
            ],
            check=True,
            **self.downloader.subprocess_additional_args,
        )

    def get_lyrics_synced_path(self, final_path: Path) -> Path:
        return final_path.with_suffix(
            SYNCED_LYRICS_FILE_EXTENSION_MAP[self.synced_lyrics_format]
        )

    def get_cover_path(self, final_path: Path, file_extension: str) -> Path:
        return final_path.parent / ("Cover" + file_extension)

    def save_lyrics_synced(self, lyrics_synced_path: Path, lyrics_synced: str):
        lyrics_synced_path.parent.mkdir(parents=True, exist_ok=True)
        lyrics_synced_path.write_text(lyrics_synced, encoding="utf8")
