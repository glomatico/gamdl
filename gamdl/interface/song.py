import base64
import datetime
import io
import json
import re
from typing import Callable
from xml.dom import minidom
from xml.etree import ElementTree

import m3u8
import structlog
from mutagen.mp4 import MP4

from .base import AppleMusicBaseInterface
from .constants import DRM_DEFAULT_KEY_MAPPING, MP4_FORMAT_CODECS, SONG_CODEC_REGEX_MAP
from .enums import MediaRating, MediaType, SongCodec, SyncedLyricsFormat
from .exceptions import (
    GamdlInterfaceDecryptionNotAvailableError,
    GamdlInterfaceFormatNotAvailableError,
    GamdlInterfaceMediaNotStreamableError,
)
from .types import (
    AppleMusicMedia,
    DecryptionKey,
    DecryptionKeyAv,
    Lyrics,
    MediaFileFormat,
    MediaTags,
    StreamInfo,
    StreamInfoAv,
)

logger = structlog.get_logger(__name__)


class AppleMusicSongInterface:
    def __init__(
        self,
        base: AppleMusicBaseInterface,
        synced_lyrics_format: SyncedLyricsFormat = SyncedLyricsFormat.LRC,
        codec_priority: list[SongCodec] = [SongCodec.AAC_LEGACY],
        use_album_date: bool = False,
        skip_decryption_key_non_legacy: bool = False,
        ask_codec_function: Callable[[list[dict]], dict] | None = None,
    ):
        self.synced_lyrics_format = synced_lyrics_format
        self.codec_priority = codec_priority
        self.use_album_date = use_album_date
        self.skip_decryption_key_non_legacy = skip_decryption_key_non_legacy
        self.ask_codec_function = ask_codec_function

        self._base = base

    async def get_lyrics(
        self,
        song_metadata: dict,
    ) -> Lyrics | None:
        log = logger.bind(
            action="get_lyrics",
            song_id=self._base.parse_catalog_media_id(song_metadata),
        )

        if not song_metadata["attributes"]["hasLyrics"]:
            log.debug("no_lyrics")
            return None

        if (
            "relationships" not in song_metadata
            or "lyrics" not in song_metadata["relationships"]
        ):
            song_metadata = (
                await self._base.apple_music_api.get_song(
                    self._base.parse_catalog_media_id(song_metadata)
                )
            )["data"][0]

        if (
            "lyrics" in song_metadata["relationships"]
            and "data" in song_metadata["relationships"]["lyrics"]
            and len(song_metadata["relationships"]["lyrics"]["data"]) > 0
            and "attributes" in song_metadata["relationships"]["lyrics"]["data"][0]
            and song_metadata["relationships"]["lyrics"]["data"][0]["attributes"].get(
                "ttml"
            )
            is not None
        ):
            lyrics = self._get_lyrics(
                song_metadata["relationships"]["lyrics"]["data"][0]["attributes"][
                    "ttml"
                ],
            )

            log.debug("success", lyrics=lyrics)

            return lyrics
        else:
            log.debug("no_lyrics_data")

    def _get_lyrics(
        self,
        lyrics_ttml: str,
    ) -> Lyrics:
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
                        synced_lyrics.append(self._get_lyrics_line_lrc(p))

                    if self.synced_lyrics_format == SyncedLyricsFormat.SRT:
                        synced_lyrics.append(self._get_lyrics_line_srt(index, p))

                    if self.synced_lyrics_format == SyncedLyricsFormat.TTML:
                        if not synced_lyrics:
                            synced_lyrics.append(
                                minidom.parseString(lyrics_ttml).toprettyxml()
                            )
                        continue

                    index += 1

        return Lyrics(
            synced="\n".join(synced_lyrics + ["\n"]) if synced_lyrics else None,
            unsynced=(
                "\n\n".join(["\n".join(lyric_group) for lyric_group in unsynced_lyrics])
                if unsynced_lyrics
                else None
            ),
        )

    def _parse_ttml_timestamp(
        self,
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

    def _get_lyrics_line_srt(self, index: int, element: ElementTree.Element) -> str:
        timestamp_begin_ttml = element.attrib.get("begin")
        timestamp_end_ttml = element.attrib.get("end")
        text = element.text

        timestamp_begin = self._parse_ttml_timestamp(timestamp_begin_ttml)
        timestamp_end = self._parse_ttml_timestamp(timestamp_end_ttml)

        return (
            f"{index}\n"
            f"{timestamp_begin.strftime('%H:%M:%S,%f')[:-3]} --> "
            f"{timestamp_end.strftime('%H:%M:%S,%f')[:-3]}\n"
            f"{text}\n"
        )

    def _get_lyrics_line_lrc(self, element: ElementTree.Element) -> str:
        timestamp_ttml = element.attrib.get("begin")
        text = element.text

        timestamp = self._parse_ttml_timestamp(timestamp_ttml)
        ms_new = timestamp.strftime("%f")[:-3]

        if int(ms_new[-1]) >= 5:
            ms = int(f"{int(ms_new[:2]) + 1}") * 10
            timestamp += datetime.timedelta(milliseconds=ms) - datetime.timedelta(
                microseconds=timestamp.microsecond
            )

        return f"[{timestamp.strftime('%M:%S.%f')[:-4]}]{text}"

    async def get_tags(
        self,
        webplayback: dict,
        lyrics: str | None = None,
    ) -> MediaTags:
        log = logger.bind(action="get_song_tags")

        webplayback_metadata = webplayback["songList"][0]["assets"][0]["metadata"]

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
                await self._base.get_media_date(webplayback_metadata["playlistId"])
                if self.use_album_date
                else (
                    self._base.parse_date(webplayback_metadata["releaseDate"])
                    if webplayback_metadata.get("releaseDate")
                    else None
                )
            ),
            disc=webplayback_metadata["discNumber"],
            disc_total=webplayback_metadata["discCount"],
            gapless=webplayback_metadata["gapless"],
            genre=webplayback_metadata.get("genre"),
            genre_id=int(webplayback_metadata["genreId"]),
            lyrics=lyrics if lyrics else None,
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

        log.debug("success", tags=tags)

        return tags

    async def get_stream_info(
        self,
        song_metadata: dict | None = None,
        webplayback: dict | None = None,
    ) -> StreamInfoAv | None:
        for codec in self.codec_priority:
            if codec.is_legacy():
                return await self._get_stream_info_legacy(webplayback, codec)
            else:
                return await self._get_stream_info(song_metadata, codec)

    async def _get_stream_info(
        self,
        song_metadata: dict,
        codec: SongCodec,
    ) -> StreamInfoAv | None:
        log = logger.bind(action="get_song_stream_info")

        if "extendedAssetUrls" not in song_metadata["attributes"]:
            song_metadata = (
                await self._base.apple_music_api.get_song(
                    self._base.parse_catalog_media_id(song_metadata),
                )
            )["data"][0]

        m3u8_master_url = song_metadata["attributes"]["extendedAssetUrls"].get(
            "enhancedHls"
        )
        if not m3u8_master_url:
            return None

        m3u8_master_obj = m3u8.loads(
            (await self._base.get_response(m3u8_master_url)).text
        )
        m3u8_master_data = m3u8_master_obj.data

        if codec == SongCodec.ASK:
            playlist = await self._get_playlist_from_user(m3u8_master_data)
        else:
            playlist = self._get_playlist_from_codec(
                m3u8_master_data,
                codec,
            )

        if playlist is None:
            log.debug("no_matching_playlist", codec=codec.value)
            return None

        stream_info = StreamInfo(legacy=False)
        stream_info.stream_url = (
            f"{m3u8_master_url.rpartition('/')[0]}/{playlist['uri']}"
        )
        stream_info.codec = playlist["stream_info"]["codecs"]
        is_mp4 = any(stream_info.codec.startswith(codec) for codec in MP4_FORMAT_CODECS)

        session_key_metadata = self._get_audio_session_key_metadata(m3u8_master_data)

        if session_key_metadata:
            asset_metadata = self._get_asset_metadata(m3u8_master_data)
            variant_id = playlist["stream_info"]["stable_variant_id"]
            drm_ids = asset_metadata[variant_id]["AUDIO-SESSION-KEY-IDS"]

            stream_info.widevine_pssh = self._get_drm_uri_from_session_key(
                session_key_metadata,
                drm_ids,
                "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",
            )
            stream_info.playready_pssh = self._get_drm_uri_from_session_key(
                session_key_metadata,
                drm_ids,
                "com.microsoft.playready",
            )
            stream_info.fairplay_key = self._get_drm_uri_from_session_key(
                session_key_metadata,
                drm_ids,
                "com.apple.streamingkeydelivery",
            )
        else:
            m3u8_obj = m3u8.loads(
                (await self._base.get_response(stream_info.stream_url)).text
            )

            stream_info.widevine_pssh = self._get_drm_uri_from_m3u8_keys(
                m3u8_obj,
                "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",
            )
            stream_info.playready_pssh = self._get_drm_uri_from_m3u8_keys(
                m3u8_obj,
                "com.microsoft.playready",
            )
            stream_info.fairplay_key = self._get_drm_uri_from_m3u8_keys(
                m3u8_obj,
                "com.apple.streamingkeydelivery",
            )

        stream_info_av = StreamInfoAv(
            audio_track=stream_info,
            file_format=MediaFileFormat.MP4 if is_mp4 else MediaFileFormat.M4A,
        )

        log.debug("success", stream_info=stream_info_av)

        return stream_info_av

    def _get_m3u8_metadata(self, m3u8_data: dict, data_id: str) -> dict | None:
        for session_data in m3u8_data.get("session_data", []):
            if session_data["data_id"] == data_id:
                return json.loads(
                    base64.b64decode(session_data["value"]).decode("utf-8")
                )
        return None

    def _get_audio_session_key_metadata(self, m3u8_data: dict) -> dict | None:
        return self._get_m3u8_metadata(
            m3u8_data,
            "com.apple.hls.AudioSessionKeyInfo",
        )

    def _get_asset_metadata(self, m3u8_data: dict) -> dict | None:
        return self._get_m3u8_metadata(
            m3u8_data,
            "com.apple.hls.audioAssetMetadata",
        )

    def _get_playlist_from_codec(
        self, m3u8_data: dict, codec: SongCodec
    ) -> dict | None:
        matching_playlists = [
            playlist
            for playlist in m3u8_data["playlists"]
            if re.fullmatch(
                SONG_CODEC_REGEX_MAP[codec.value], playlist["stream_info"]["audio"]
            )
        ]

        if not matching_playlists:
            return None

        return max(
            matching_playlists,
            key=lambda x: x["stream_info"]["average_bandwidth"],
        )

    async def _get_playlist_from_user(self, m3u8_data: dict) -> dict | None:
        if not self.ask_codec_function:
            return None

        return self.ask_codec_function(
            [playlist["stream_info"] for playlist in m3u8_data["playlists"]]
        )

    def _get_drm_uri_from_session_key(
        self,
        drm_infos: dict,
        drm_ids: list,
        drm_key: str,
    ) -> str | None:
        for drm_id in drm_ids:
            if drm_id != "1" and drm_key in drm_infos.get(drm_id, {}):
                return drm_infos[drm_id][drm_key]["URI"]
        return None

    def _get_drm_uri_from_m3u8_keys(
        self,
        m3u8_obj: m3u8.M3U8,
        drm_key: str,
    ) -> str | None:
        default_uri = DRM_DEFAULT_KEY_MAPPING[drm_key]

        for key in m3u8_obj.keys:
            if key.keyformat == drm_key and key.uri != default_uri:
                return key.uri
        return None

    async def _get_stream_info_legacy(
        self,
        webplayback: dict,
        codec: SongCodec,
    ) -> StreamInfoAv:
        log = logger.bind(action="get_legacy_song_stream_info")

        flavor = "32:ctrp64" if codec == SongCodec.AAC_HE_LEGACY else "28:ctrp256"

        stream_info = StreamInfo(legacy=True)
        stream_info.stream_url = next(
            i for i in webplayback["songList"][0]["assets"] if i["flavor"] == flavor
        )["URL"]

        m3u8_obj = m3u8.loads(
            (await self._base.get_response(stream_info.stream_url)).text
        )
        stream_info.widevine_pssh = m3u8_obj.keys[0].uri

        stream_info_av = StreamInfoAv(
            media_id=webplayback["songList"][0]["songId"],
            audio_track=stream_info,
            file_format=MediaFileFormat.M4A,
        )
        log.debug("success", stream_info=stream_info_av)

        return stream_info_av

    async def get_media(
        self,
        song_metadata: dict,
        playlist_metadata: dict | None = None,
        playlist_track: int | None = None,
    ) -> AppleMusicMedia:
        media = AppleMusicMedia(
            media_id=self._base.parse_catalog_media_id(song_metadata),
            media_metadata=song_metadata,
        )

        if not self._base.is_media_streamable(song_metadata):
            raise GamdlInterfaceMediaNotStreamableError(
                media_id=media.media_id,
            )

        if playlist_metadata and playlist_track:
            media.playlist_metadata = playlist_metadata
            media.playlist_tags = self._base.get_playlist_tags(
                playlist_metadata,
                playlist_track,
            )

        media.cover = await self._base.get_cover(song_metadata)

        media.lyrics = await self.get_lyrics(song_metadata)

        webplayback = await self._base.apple_music_api.get_webplayback(media.media_id)

        media.tags = await self.get_tags(
            webplayback,
            media.lyrics.unsynced if media.lyrics else None,
        )

        media.stream_info = await self.get_stream_info(
            song_metadata,
            webplayback,
        )
        if not media.stream_info:
            raise GamdlInterfaceFormatNotAvailableError(
                media_id=media.media_id,
                codec=self.codec_priority,
            )

        if (
            not self.skip_decryption_key_non_legacy
            and not media.stream_info.audio_track.widevine_pssh
        ) or (
            self.skip_decryption_key_non_legacy
            and not media.stream_info.audio_track.fairplay_key
        ):
            raise GamdlInterfaceDecryptionNotAvailableError(media_id=media.media_id)

        if (
            media.stream_info.audio_track.widevine_pssh
            and not self.skip_decryption_key_non_legacy
        ) or media.stream_info.audio_track.legacy:
            media.decryption_key = DecryptionKeyAv(
                audio_track=await self._base.get_decryption_key(
                    media.stream_info.audio_track.widevine_pssh,
                    media.media_id,
                )
            )

        return media
