import asyncio
import urllib.parse
from typing import AsyncGenerator, Callable

import m3u8
import structlog

from .base import AppleMusicBaseInterface
from .constants import MP4_FORMAT_CODECS
from .enums import MediaRating, MediaType, MusicVideoCodec, MusicVideoResolution
from .exceptions import (
    GamdlInterfaceDecryptionNotAvailableError,
    GamdlInterfaceFormatNotAvailableError,
    GamdlInterfaceMediaNotStreamableError,
)
from .types import (
    AppleMusicMedia,
    DecryptionKeyAv,
    MediaFileFormat,
    MediaTags,
    StreamInfo,
    StreamInfoAv,
)

logger = structlog.get_logger(__name__)


class AppleMusicMusicVideoInterface:
    def __init__(
        self,
        base: AppleMusicBaseInterface,
        resolution: MusicVideoResolution = MusicVideoResolution.R1080P,
        codec_priority: list[MusicVideoCodec] = [
            MusicVideoCodec.H264,
            MusicVideoCodec.H265,
        ],
        ask_video_codec_function: (
            Callable[[list[m3u8.Playlist]], dict | None] | None
        ) = None,
        ask_audio_codec_function: Callable[[list[dict]], dict | None] | None = None,
    ):
        self.base = base
        self.resolution = resolution
        self.codec_priority = codec_priority
        self.ask_video_codec_function = ask_video_codec_function
        self.ask_audio_codec_function = ask_audio_codec_function

    async def get_itunes_page_metadata(
        self,
        music_video_metadata: dict,
    ) -> dict:
        url_media_id = self.base.parse_media_id_from_url(music_video_metadata)
        itunes_page = await self.base.itunes_api.get_itunes_page(
            "music-video",
            url_media_id,
        )
        return itunes_page["storePlatformData"]["product-dv"]["results"][url_media_id]

    def _get_m3u8_master_url_from_webplayback(self, webplayback: dict) -> str:
        m3u8_master_url = webplayback["hls-playlist-url"]
        return m3u8_master_url

    def _get_m3u8_master_url_from_itunes_page_metadata(
        self,
        itunes_page_metadata: dict,
    ) -> str | None:
        stream_url = itunes_page_metadata["offers"][0]["assets"][0].get("hlsUrl")
        if not stream_url:
            return None

        url_parts = urllib.parse.urlparse(stream_url)
        query = urllib.parse.parse_qs(url_parts.query, keep_blank_values=True)
        query.update({"aec": "HD", "dsid": "1"})

        m3u8_master_url = url_parts._replace(
            query=urllib.parse.urlencode(query, doseq=True)
        ).geturl()

        return m3u8_master_url

    async def get_tags(
        self,
        metadata: dict,
        itunes_page_metadata: dict,
    ) -> MediaTags:
        log = logger.bind(
            action="get_music_video_tags",
            media_id=self.base.parse_catalog_media_id(metadata),
        )

        url_media_id = self.base.parse_media_id_from_url(metadata)
        lookup_metadata = (await self.base.itunes_api.get_lookup_result(url_media_id))[
            "results"
        ]

        explicitness = lookup_metadata[0]["trackExplicitness"]
        if explicitness == "notExplicit":
            rating = MediaRating.NONE
        elif explicitness == "explicit":
            rating = MediaRating.EXPLICIT
        else:
            rating = MediaRating.CLEAN

        tags = MediaTags(
            artist=lookup_metadata[0]["artistName"],
            artist_id=int(lookup_metadata[0]["artistId"]),
            copyright=itunes_page_metadata.get("copyright"),
            date=self.base.parse_date(lookup_metadata[0]["releaseDate"]),
            genre=lookup_metadata[0]["primaryGenreName"],
            genre_id=int(itunes_page_metadata["genres"][0]["genreId"]),
            media_type=MediaType.MUSIC_VIDEO,
            storefront=self.base.itunes_api.storefront_id,
            title=lookup_metadata[0]["trackCensoredName"],
            title_id=int(metadata["id"]),
            rating=rating,
        )

        if len(lookup_metadata) > 1:
            album = await self.base.get_album_cached(
                itunes_page_metadata["collectionId"]
            )
            if not album:
                return tags

            tags.album = lookup_metadata[1]["collectionCensoredName"]
            tags.album_artist = lookup_metadata[1]["artistName"]
            tags.album_id = int(itunes_page_metadata["collectionId"])
            tags.disc = lookup_metadata[0]["discNumber"]
            tags.disc_total = lookup_metadata[0]["discCount"]
            tags.compilation = album["attributes"]["isCompilation"]
            tags.track = lookup_metadata[0]["trackNumber"]
            tags.track_total = lookup_metadata[0]["trackCount"]

        log.debug("success", tags=tags)

        return tags

    async def get_stream_info(
        self,
        metadata: dict,
        itunes_page_metadata: dict,
    ) -> StreamInfoAv | None:
        log = logger.bind(
            action="get_music_video_stream_info",
            media_id=self.base.parse_catalog_media_id(metadata),
        )

        url_media_id = self.base.parse_media_id_from_url(metadata)
        m3u8_master_url = None

        if url_media_id == metadata["id"]:
            m3u8_master_url = self._get_m3u8_master_url_from_itunes_page_metadata(
                itunes_page_metadata,
            )

        if not m3u8_master_url:
            webplayback_response = await self.base.apple_music_api.get_webplayback(
                metadata["id"]
            )
            m3u8_master_url = self._get_m3u8_master_url_from_webplayback(
                webplayback_response["songList"][0],
            )

        playlist_master_m3u8_obj = m3u8.loads(
            (await self.base.get_response(m3u8_master_url)).text
        )
        playlist_master_m3u8_obj.base_uri = m3u8_master_url.rpartition("/")[0]
        stream_info_video = await self._get_stream_info_video(playlist_master_m3u8_obj)
        stream_info_audio = await self._get_stream_info_audio(
            playlist_master_m3u8_obj.data,
        )
        if not stream_info_video or not stream_info_audio:
            return None

        use_mp4 = any(
            stream_info_video.codec.startswith(codec) for codec in MP4_FORMAT_CODECS
        ) or any(
            stream_info_audio.codec.startswith(codec) for codec in MP4_FORMAT_CODECS
        )
        if use_mp4:
            file_format = MediaFileFormat.MP4
        else:
            file_format = MediaFileFormat.M4V

        stream_info = StreamInfoAv(
            video_track=stream_info_video,
            audio_track=stream_info_audio,
            file_format=file_format,
        )

        log.debug("success", stream_info=stream_info)

        return stream_info

    def _get_video_playlist_from_resolution(
        self,
        video_playlists: list[m3u8.Playlist],
    ) -> m3u8.Playlist | None:
        playlist_results = []
        for codec_index, codec in enumerate(self.codec_priority):
            for playlist in video_playlists:
                if playlist.stream_info.codecs.startswith(codec.fourcc()):
                    playlist_results.append((codec_index, playlist))

        if not playlist_results:
            return None

        def sort_key(
            item: tuple[int, m3u8.Playlist],
        ) -> tuple[bool, int, int, int, int]:
            codec_index, playlist = item
            playlist_resolution = playlist.stream_info.resolution[-1]
            bandwidth = playlist.stream_info.bandwidth
            exceeds_resolution = playlist_resolution > int(self.resolution)
            resolution_difference = abs(playlist_resolution - int(self.resolution))

            return (
                exceeds_resolution,
                resolution_difference,
                codec_index,
                -playlist_resolution,
                -bandwidth,
            )

        playlist_results.sort(key=sort_key)
        return playlist_results[0][1]

    def _get_best_stereo_audio_playlist(
        self,
        playlist_master_data: dict,
    ) -> dict | None:
        audio_playlist = next(
            (
                media
                for media in playlist_master_data["media"]
                if media["group_id"] == "audio-stereo-256"
            ),
            None,
        )
        return audio_playlist

    async def _get_video_playlist_from_user(
        self,
        video_playlists: list[m3u8.Playlist],
    ) -> m3u8.Playlist | None:
        if self.ask_video_codec_function:
            video_playlist = self.ask_video_codec_function(video_playlists)
            if asyncio.iscoroutine(video_playlist):
                video_playlist = await video_playlist

            return video_playlist

        return None

    async def _get_audio_playlist_from_user(
        self,
        playlist_master_data: dict,
    ) -> dict | None:
        if self.ask_audio_codec_function:
            audio_playlist = self.ask_audio_codec_function(
                [
                    playlist
                    for playlist in playlist_master_data["media"]
                    if playlist.get("uri")
                ]
            )
            if asyncio.iscoroutine(audio_playlist):
                audio_playlist = await audio_playlist

            return audio_playlist

        return None

    def _get_key_by_format(
        self,
        m3u8_obj: m3u8.M3U8,
        key_format: str,
    ) -> str:
        return next(
            (key for key in m3u8_obj.keys if key.keyformat == key_format),
            None,
        ).uri

    def _get_widevine_pssh(self, m3u8_obj: m3u8.M3U8) -> str:
        return self._get_key_by_format(
            m3u8_obj,
            "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",
        )

    def _get_playready_pssh(self, m3u8_obj: m3u8.M3U8) -> str:
        return self._get_key_by_format(
            m3u8_obj,
            "com.microsoft.playready",
        )

    def _get_fairplay_key(self, m3u8_obj: m3u8.M3U8) -> str:
        return self._get_key_by_format(
            m3u8_obj,
            "com.apple.streamingkeydelivery",
        )

    async def _get_stream_info_video(
        self,
        playlist_master_m3u8_obj: m3u8.M3U8,
    ) -> StreamInfo | None:
        stream_info = StreamInfo()

        if MusicVideoCodec.ASK not in self.codec_priority:
            playlist = self._get_video_playlist_from_resolution(
                playlist_master_m3u8_obj.playlists,
            )
        else:
            playlist = await self._get_video_playlist_from_user(
                playlist_master_m3u8_obj.playlists
            )

        if not playlist:
            return None

        stream_info.stream_url = playlist.uri
        stream_info.codec = playlist.stream_info.codecs
        stream_info.width, stream_info.height = playlist.stream_info.resolution

        playlist_m3u8_obj = m3u8.loads(
            (await self.base.get_response(stream_info.stream_url)).text
        )
        stream_info.widevine_pssh = self._get_widevine_pssh(playlist_m3u8_obj)
        stream_info.fairplay_key = self._get_fairplay_key(playlist_m3u8_obj)
        stream_info.playready_pssh = self._get_playready_pssh(playlist_m3u8_obj)

        return stream_info

    async def _get_stream_info_audio(
        self,
        playlist_master_data: dict,
    ) -> StreamInfo | None:
        stream_info = StreamInfo()

        if MusicVideoCodec.ASK not in self.codec_priority:
            playlist = self._get_best_stereo_audio_playlist(playlist_master_data)
        else:
            playlist = await self._get_audio_playlist_from_user(playlist_master_data)

        if not playlist:
            return None

        stream_info.stream_url = playlist["uri"]
        stream_info.codec = playlist["group_id"]

        playlist_m3u8_obj = m3u8.loads(
            (await self.base.get_response(stream_info.stream_url)).text
        )
        stream_info.widevine_pssh = self._get_widevine_pssh(playlist_m3u8_obj)
        stream_info.fairplay_key = self._get_fairplay_key(playlist_m3u8_obj)
        stream_info.playready_pssh = self._get_playready_pssh(playlist_m3u8_obj)

        return stream_info

    async def get_decryption_key(
        self,
        stream_info: StreamInfoAv,
    ) -> DecryptionKeyAv:
        decryption_key_video, decryption_key_audio = await asyncio.gather(
            self.base.get_decryption_key(
                stream_info.video_track.widevine_pssh,
                stream_info.media_id,
            ),
            self.base.get_decryption_key(
                stream_info.audio_track.widevine_pssh,
                stream_info.media_id,
            ),
        )

        return DecryptionKeyAv(
            video_track=decryption_key_video,
            audio_track=decryption_key_audio,
        )

    async def get_media(
        self,
        media: AppleMusicMedia,
    ) -> AsyncGenerator[AppleMusicMedia, None]:
        if not media.media_metadata:
            media.media_metadata = (
                await self.base.apple_music_api.get_music_video(media.media_id)
            )["data"][0]

        media.media_id = self.base.parse_catalog_media_id(media.media_metadata)

        yield media

        if not self.base.is_media_streamable(media.media_metadata):
            raise GamdlInterfaceMediaNotStreamableError(media.media_id)

        if media.playlist_metadata:
            media.playlist_tags = self.base.get_playlist_tags(
                media.playlist_metadata,
                media.index,
            )

        media.cover = await self.base.get_cover(media.media_metadata)

        itunes_page_metadata = await self.get_itunes_page_metadata(media.media_metadata)
        media.tags = await self.get_tags(
            media.media_metadata,
            itunes_page_metadata,
        )

        media.stream_info = await self.get_stream_info(
            media.media_metadata,
            itunes_page_metadata,
        )
        if not media.stream_info:
            raise GamdlInterfaceFormatNotAvailableError(
                media.media_id,
                self.codec_priority,
            )

        if (
            not media.stream_info.video_track.widevine_pssh
            or not media.stream_info.audio_track.widevine_pssh
        ):
            raise GamdlInterfaceDecryptionNotAvailableError(media.media_id)

        media.decryption_key = await self.get_decryption_key(media.stream_info)

        media.partial = False

        yield media
