from __future__ import annotations

import base64
import datetime
import json
import logging
import re
import subprocess
import typing
from pathlib import Path
from xml.dom import minidom
from xml.etree import ElementTree

import colorama
import m3u8
from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from pywidevine import PSSH
from pywidevine.license_protocol_pb2 import WidevinePsshData

from .downloader import Downloader
from .enums import MediaFileFormat, RemuxMode, SongCodec, SyncedLyricsFormat
from .exceptions import *
from .models import (
    DecryptionKey,
    DecryptionKeyAv,
    DownloadInfo,
    Lyrics,
    MediaRating,
    MediaTags,
    MediaType,
    StreamInfo,
    StreamInfoAv,
)
from .utils import color_text

logger = logging.getLogger("gamdl")


class DownloaderSong:
    DEFAULT_DECRYPTION_KEY = "32b8ade1769e26b1ffb8986352793fc6"
    MP4_FORMAT_CODECS = ["ec-3"]
    SONG_CODEC_REGEX_MAP = {
        SongCodec.AAC: r"audio-stereo-\d+",
        SongCodec.AAC_HE: r"audio-HE-stereo-\d+",
        SongCodec.AAC_BINAURAL: r"audio-stereo-\d+-binaural",
        SongCodec.AAC_DOWNMIX: r"audio-stereo-\d+-downmix",
        SongCodec.AAC_HE_BINAURAL: r"audio-HE-stereo-\d+-binaural",
        SongCodec.AAC_HE_DOWNMIX: r"audio-HE-stereo-\d+-downmix",
        SongCodec.ATMOS: r"audio-atmos-.*",
        SongCodec.AC3: r"audio-ac3-.*",
        SongCodec.ALAC: r"audio-alac-.*",
    }
    DRM_DEFAULT_KEY_MAPPING = {
        "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed": (
            "data:text/plain;base64,AAAAOHBzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAABgSEAAAAAA"
            "AAAAAczEvZTEgICBI88aJmwY="
        ),
        "com.microsoft.playready": (
            "data:text/plain;charset=UTF-16;base64,vgEAAAEAAQC0ATwAVwBSAE0ASABFAEEARABF"
            "AFIAIAB4AG0AbABuAHMAPQAiAGgAdAB0AHAAOgAvAC8AcwBjAGgAZQBtAGEAcwAuAG0AaQBjAH"
            "IAbwBzAG8AZgB0AC4AYwBvAG0ALwBEAFIATQAvADIAMAAwADcALwAwADMALwBQAGwAYQB5AFIA"
            "ZQBhAGQAeQBIAGUAYQBkAGUAcgAiACAAdgBlAHIAcwBpAG8AbgA9ACIANAAuADMALgAwAC4AMA"
            "AiAD4APABEAEEAVABBAD4APABQAFIATwBUAEUAQwBUAEkATgBGAE8APgA8AEsASQBEAFMAPgA8"
            "AEsASQBEACAAQQBMAEcASQBEAD0AIgBBAEUAUwBDAEIAQwAiACAAVgBBAEwAVQBFAD0AIgBBAE"
            "EAQQBBAEEAQQBBAEEAQQBBAEIAegBNAFMAOQBsAE0AUwBBAGcASQBBAD0APQAiAD4APAAvAEsA"
            "SQBEAD4APAAvAEsASQBEAFMAPgA8AC8AUABSAE8AVABFAEMAVABJAE4ARgBPAD4APAAvAEQAQQ"
            "BUAEEAPgA8AC8AVwBSAE0ASABFAEEARABFAFIAPgA="
        ),
        "com.apple.streamingkeydelivery": "skd://itunes.apple.com/P000000000/s1/e1",
    }

    def __init__(
        self,
        downloader: Downloader,
        codec: SongCodec = SongCodec.AAC_LEGACY,
        synced_lyrics_format: SyncedLyricsFormat = SyncedLyricsFormat.LRC,
    ):
        self.downloader = downloader
        self.codec = codec
        self.synced_lyrics_format = synced_lyrics_format

    def _search_m3u8_metadata(self, m3u8_data: dict, data_id: str) -> dict:
        searched = next(
            (
                session_data
                for session_data in m3u8_data["session_data"]
                if session_data["data_id"] == data_id
            ),
            None,
        )
        if not searched:
            return None
        return json.loads(base64.b64decode(searched["value"]).decode("utf-8"))

    def get_audio_session_key_metadata(self, m3u8_data: dict) -> dict:
        return self._search_m3u8_metadata(
            m3u8_data,
            "com.apple.hls.AudioSessionKeyInfo",
        )

    def get_asset_metadata(self, m3u8_data: dict) -> dict:
        return self._search_m3u8_metadata(
            m3u8_data,
            "com.apple.hls.audioAssetMetadata",
        )

    def get_playlist_from_codec(self, m3u8_data: dict) -> dict | None:
        m3u8_master_playlists = [
            playlist
            for playlist in m3u8_data["playlists"]
            if re.fullmatch(
                self.SONG_CODEC_REGEX_MAP[self.codec], playlist["stream_info"]["audio"]
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

    def _get_drm_uri_from_session_key(
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

    def _get_drm_uri_from_m3u8_keys(
        self,
        m3u8_obj: m3u8.M3U8,
        drm_key: str,
    ) -> str | None:
        drm_uri = next(
            (
                key
                for key in m3u8_obj.keys
                if key.keyformat == drm_key
                and key.uri != self.DRM_DEFAULT_KEY_MAPPING[drm_key]
            ),
            None,
        )
        if not drm_uri:
            return None
        return drm_uri.uri

    def _get_stream_info(self, m3u8_url: str) -> StreamInfoAv | None:
        stream_info = StreamInfo()
        m3u8_master_obj = m3u8.load(m3u8_url)
        m3u8_master_data = m3u8_master_obj.data

        if self.codec == SongCodec.ASK:
            playlist = self.get_playlist_from_user(m3u8_master_data)
        else:
            playlist = self.get_playlist_from_codec(m3u8_master_data)
        if playlist is None:
            return None
        stream_info.stream_url = m3u8_master_obj.base_uri + playlist["uri"]

        stream_info.codec = playlist["stream_info"]["codecs"]
        is_mp4 = any(
            stream_info.codec.startswith(possible_codec)
            for possible_codec in self.MP4_FORMAT_CODECS
        )

        session_key_metadata = self.get_audio_session_key_metadata(m3u8_master_data)
        if session_key_metadata:
            asset_metadata = self.get_asset_metadata(m3u8_master_data)
            variant_id = playlist["stream_info"]["stable_variant_id"]
            drm_ids = asset_metadata[variant_id]["AUDIO-SESSION-KEY-IDS"]
            (
                stream_info.widevine_pssh,
                stream_info.playready_pssh,
                stream_info.fairplay_key,
            ) = (
                self._get_drm_uri_from_session_key(
                    session_key_metadata,
                    drm_ids,
                    drm_key,
                )
                for drm_key in self.DRM_DEFAULT_KEY_MAPPING.keys()
            )
        else:
            m3u8_obj = m3u8.load(stream_info.stream_url)
            (
                stream_info.widevine_pssh,
                stream_info.playready_pssh,
                stream_info.fairplay_key,
            ) = (
                self._get_drm_uri_from_m3u8_keys(
                    m3u8_obj,
                    drm_key,
                )
                for drm_key in self.DRM_DEFAULT_KEY_MAPPING.keys()
            )

        return StreamInfoAv(
            audio_track=stream_info,
            file_format=MediaFileFormat.MP4 if is_mp4 else MediaFileFormat.M4A,
        )

    def get_stream_info(self, track_metadata: dict) -> StreamInfoAv | None:
        m3u8_url = track_metadata["attributes"]["extendedAssetUrls"].get("enhancedHls")
        if not m3u8_url:
            return None
        return self._get_stream_info(m3u8_url)

    def get_stream_info_legacy(self, webplayback: dict) -> StreamInfoAv:
        flavor = "32:ctrp64" if self.codec == SongCodec.AAC_HE_LEGACY else "28:ctrp256"

        stream_info = StreamInfo()
        stream_info.stream_url = next(
            i for i in webplayback["assets"] if i["flavor"] == flavor
        )["URL"]

        m3u8_obj = m3u8.load(stream_info.stream_url)
        stream_info.widevine_pssh = m3u8_obj.keys[0].uri

        return StreamInfoAv(
            audio_track=stream_info,
            file_format=MediaFileFormat.M4A,
        )

    def get_decryption_key(
        self,
        stream_info: StreamInfoAv,
        media_id: str,
    ) -> DecryptionKeyAv:
        decryption_key = self.downloader.get_decryption_key(
            stream_info.audio_track.widevine_pssh,
            media_id,
        )
        return DecryptionKeyAv(
            audio_track=decryption_key,
        )

    def get_decryption_key_legacy(
        self,
        stream_info: StreamInfoAv,
        media_id: str,
    ) -> DecryptionKeyAv:
        stream_info_audio = stream_info.audio_track

        try:
            cdm_session = self.downloader.cdm.open()

            widevine_pssh_data = WidevinePsshData()
            widevine_pssh_data.algorithm = 1
            widevine_pssh_data.key_ids.append(
                base64.b64decode(stream_info_audio.widevine_pssh.split(",")[1])
            )
            pssh_obj = PSSH(widevine_pssh_data.SerializeToString())

            challenge = base64.b64encode(
                self.downloader.cdm.get_license_challenge(cdm_session, pssh_obj)
            ).decode()
            license = self.downloader.apple_music_api.get_widevine_license(
                media_id,
                stream_info.audio_track.widevine_pssh,
                challenge,
            )

            self.downloader.cdm.parse_license(cdm_session, license)
            decryption_key = next(
                i
                for i in self.downloader.cdm.get_keys(cdm_session)
                if i.type == "CONTENT"
            )
        finally:
            self.downloader.cdm.close(cdm_session)
        return DecryptionKeyAv(
            audio_track=DecryptionKey(
                kid=decryption_key.kid.hex,
                key=decryption_key.key.hex(),
            )
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
                self.downloader.get_media_id_of_library_media(track_metadata)
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
        lyrics_ttml_et = ElementTree.fromstring(lyrics_ttml)
        unsynced_lyrics = []
        synced_lyrics = []
        index = 1
        for div in lyrics_ttml_et.iter("{http://www.w3.org/ns/ttml}div"):
            stanza = []
            unsynced_lyrics.append(stanza)

            for p in div.iter("{http://www.w3.org/ns/ttml}p"):
                if p.text is not None:
                    stanza.append(p.text)

                if p.attrib.get("begin"):
                    if self.synced_lyrics_format == SyncedLyricsFormat.LRC:
                        synced_lyrics.append(
                            f"{self.get_lyrics_synced_line_lrc(p.attrib.get('begin'), p.text)}"
                        )

                    if self.synced_lyrics_format == SyncedLyricsFormat.SRT:
                        synced_lyrics.append(
                            f"{self.get_lyrics_synced_line_srt(index, p.attrib.get('begin'), p.attrib.get('end'), p.text)}"
                        )

                    if self.synced_lyrics_format == SyncedLyricsFormat.TTML:
                        if not synced_lyrics:
                            synced_lyrics.append(
                                minidom.parseString(lyrics_ttml).toprettyxml()
                            )
                        continue

                    index += 1

        return Lyrics(
            synced="\n".join(synced_lyrics) + "\n",
            unsynced="\n\n".join(
                ["\n".join(lyric_group) for lyric_group in unsynced_lyrics]
            ),
        )

    def get_tags(self, webplayback: dict, lyrics_unsynced: str) -> MediaTags:
        webplayback_metadata = webplayback["assets"][0]["metadata"]
        tags = MediaTags(
            album=webplayback_metadata["playlistName"],
            album_artist=webplayback_metadata["playlistArtistName"],
            album_id=int(webplayback_metadata["playlistId"]),
            album_sort=webplayback_metadata["sort-album"],
            artist=webplayback_metadata["artistName"],
            artist_id=int(webplayback_metadata["artistId"]),
            artist_sort=webplayback_metadata["sort-artist"],
            comment=webplayback_metadata.get("comments"),
            compilation=webplayback_metadata["compilation"],
            composer=webplayback_metadata.get("composerName"),
            composer_id=(
                int(webplayback_metadata.get("composerId"))
                if webplayback_metadata.get("composerId")
                else None
            ),
            composer_sort=webplayback_metadata.get("sort-composer"),
            copyright=webplayback_metadata.get("copyright"),
            date=(
                self.downloader.parse_date(webplayback_metadata["releaseDate"])
                if webplayback_metadata.get("releaseDate")
                else None
            ),
            disc=webplayback_metadata["discNumber"],
            disc_total=webplayback_metadata["discCount"],
            gapless=webplayback_metadata["gapless"],
            genre=webplayback_metadata.get("genre"),
            genre_id=int(webplayback_metadata["genreId"]),
            lyrics=lyrics_unsynced if lyrics_unsynced else None,
            media_type=MediaType.SONG,
            rating=MediaRating(webplayback_metadata["explicit"]),
            storefront=webplayback_metadata["s"],
            title=webplayback_metadata["itemName"],
            title_id=int(webplayback_metadata["itemId"]),
            title_sort=webplayback_metadata["sort-name"],
            track=webplayback_metadata["trackNumber"],
            track_total=webplayback_metadata["trackCount"],
            xid=webplayback_metadata.get("xid"),
        )
        return tags

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
        codec: SongCodec,
    ):
        if codec.is_legacy():
            keys = [
                "--key",
                f"1:{decryption_key}",
            ]
        else:
            self.fix_key_id(encrypted_path)
            keys = [
                "--key",
                "0" * 31 + "1" + f":{decryption_key}",
                "--key",
                "0" * 32 + f":{self.DEFAULT_DECRYPTION_KEY}",
            ]
        subprocess.run(
            [
                self.downloader.mp4decrypt_path_full,
                *keys,
                encrypted_path,
                decrypted_path,
            ],
            check=True,
            **self.downloader.subprocess_additional_args,
        )

    def stage(
        self,
        codec: SongCodec,
        encrypted_path: Path,
        decrypted_path: Path,
        decryption_key: DecryptionKeyAv,
        staged_path: Path,
    ):
        if codec.is_legacy() and self.downloader.remux_mode == RemuxMode.FFMPEG:
            self.remux_ffmpeg(
                encrypted_path,
                staged_path,
                decryption_key.audio_track.key,
            )
        else:
            self.decrypt(
                encrypted_path,
                decrypted_path,
                decryption_key.audio_track.key,
                codec,
            )
            if self.downloader.remux_mode == RemuxMode.FFMPEG:
                self.remux_ffmpeg(
                    decrypted_path,
                    staged_path,
                )
            else:
                self.remux_mp4box(
                    decrypted_path,
                    staged_path,
                )

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
        decryption_key: str = None,
    ):
        if decryption_key:
            decryption_key_arg = [
                "-decryption_key",
                decryption_key,
            ]
        else:
            decryption_key_arg = []
        subprocess.run(
            [
                self.downloader.ffmpeg_path_full,
                "-loglevel",
                "error",
                "-y",
                *decryption_key_arg,
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
        return final_path.with_suffix("." + self.synced_lyrics_format.value)

    def get_cover_path(self, final_path: Path, cover_format: str) -> Path:
        return final_path.parent / (
            "Cover" + self.downloader.get_cover_file_extension(cover_format)
        )

    def save_lyrics_synced(self, lyrics_synced_path: Path, lyrics_synced: str):
        lyrics_synced_path.parent.mkdir(parents=True, exist_ok=True)
        lyrics_synced_path.write_text(lyrics_synced, encoding="utf8")

    def download(
        self,
        media_id: str = None,
        media_metadata: dict = None,
        playlist_attributes: dict = None,
        playlist_track: int = None,
    ) -> typing.Generator[DownloadInfo, None, None]:
        yield from self.downloader._final_processing_wrapper(
            self._download,
            media_id,
            media_metadata,
            playlist_attributes,
            playlist_track,
        )

    def _download(
        self,
        media_id: str = None,
        media_metadata: dict = None,
        playlist_attributes: dict = None,
        playlist_track: int = None,
    ) -> typing.Generator[DownloadInfo, None, None]:
        download_info = DownloadInfo()
        yield download_info

        if playlist_track is None and playlist_attributes:
            raise ValueError(
                "playlist_track must be provided if playlist_attributes is provided"
            )
        if playlist_attributes:
            playlist_tags = self.downloader.get_playlist_tags(
                playlist_attributes,
                playlist_track,
            )
        else:
            playlist_tags = None
        download_info.playlist_tags = playlist_tags

        if not media_id and not media_metadata:
            raise ValueError("Either media_id or media_metadata must be provided")

        if media_metadata:
            media_id = self.downloader.get_media_id_of_library_media(media_metadata)
        download_info.media_id = media_id
        colored_media_id = color_text(media_id, colorama.Style.DIM)

        database_final_path = self.downloader.get_database_final_path(media_id)
        if database_final_path:
            download_info.final_path = database_final_path
            yield download_info
            raise MediaFileAlreadyExistsException(database_final_path)

        if not media_metadata:
            logger.debug(f"[{colored_media_id}] Getting Song metadata")
            media_metadata = self.downloader.apple_music_api.get_song(media_id)
        download_info.media_metadata = media_metadata

        if not self.downloader.is_media_streamable(media_metadata):
            raise MediaNotStreamableException()

        logger.debug(f"[{colored_media_id}] Getting lyrics")
        lyrics = self.get_lyrics(media_metadata)
        download_info.lyrics = lyrics

        logger.debug(f"[{colored_media_id}] Getting webplayback info")
        webplayback = self.downloader.apple_music_api.get_webplayback(
            media_id,
        )
        tags = self.get_tags(
            webplayback,
            lyrics.unsynced if lyrics else None,
        )
        final_path = self.downloader.get_final_path(tags, ".m4a", playlist_tags)
        download_info.tags = tags
        download_info.final_path = final_path

        if lyrics and lyrics.synced:
            synced_lyrics_path = self.get_lyrics_synced_path(final_path)
        else:
            synced_lyrics_path = None
        download_info.synced_lyrics_path = synced_lyrics_path

        if self.downloader.synced_lyrics_only:
            logger.info(
                f"[{colored_media_id}] Downloading synced lyrics only, skipping song download"
            )
            yield download_info
            return

        cover_url = self.downloader.get_cover_url(media_metadata)
        cover_format = self.downloader.get_cover_format(cover_url)
        if cover_format:
            cover_path = self.get_cover_path(final_path, cover_format)
        else:
            cover_path = None
        download_info.cover_url = cover_url
        download_info.cover_format = cover_format
        download_info.cover_path = cover_path

        if final_path.exists() and not self.downloader.overwrite:
            yield download_info
            raise MediaFileAlreadyExistsException(final_path)

        logger.debug(f"[{colored_media_id}] Getting stream info")
        if self.codec.is_legacy():
            stream_info = self.get_stream_info_legacy(webplayback)
            logger.debug(f"[{colored_media_id}] Getting decryption key")
            decryption_key = self.get_decryption_key_legacy(
                stream_info,
                media_id,
            )
            download_info.stream_info = stream_info
            download_info.decryption_key = decryption_key
        else:
            stream_info = self.get_stream_info(media_metadata)

            if not stream_info or not stream_info.audio_track.widevine_pssh:
                yield download_info
                raise MediaFormatNotAvailableException()

            logger.debug(f"[{colored_media_id}] Getting decryption key")
            decryption_key = self.get_decryption_key(
                stream_info,
                media_id,
            )
        download_info.stream_info = stream_info
        download_info.decryption_key = decryption_key

        encrypted_path = self.downloader.get_temp_path(
            media_id,
            "encrypted",
            ".m4a",
        )
        decrypted_path = self.downloader.get_temp_path(
            media_id,
            "decrypted",
            ".m4a",
        )
        staged_path = self.downloader.get_temp_path(
            media_id,
            "staged",
            self.downloader.get_media_file_extension(stream_info.file_format),
        )

        logger.info(f"[{colored_media_id}] Downloading song")

        logger.debug(f'[{colored_media_id}] Downloading to "{encrypted_path}"')
        self.downloader.download(
            encrypted_path,
            download_info.stream_info.audio_track.stream_url,
        )

        logger.debug(
            f'[{colored_media_id}] Decryping/remuxing to "{decrypted_path}"/"{staged_path}"'
        )
        self.stage(
            self.codec,
            encrypted_path,
            decrypted_path,
            decryption_key,
            staged_path,
        )
        download_info.staged_path = staged_path

        yield download_info
