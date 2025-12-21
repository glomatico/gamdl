import logging
import urllib.parse

import m3u8
from async_lru import alru_cache
from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from pywidevine import Cdm

from ..utils import get_response
from .constants import MP4_FORMAT_CODECS
from .enums import MediaRating, MediaType, MusicVideoCodec, MusicVideoResolution
from .interface import AppleMusicInterface
from .types import DecryptionKeyAv, MediaFileFormat, MediaTags, StreamInfo, StreamInfoAv

logger = logging.getLogger(__name__)


class AppleMusicMusicVideoInterface(AppleMusicInterface):
    def __init__(self, interface: AppleMusicInterface):
        self.__dict__.update(interface.__dict__)

    async def get_itunes_page_metadata(
        self,
        music_video_metadata: dict,
    ) -> dict:
        alt_id = self.get_alt_id(music_video_metadata)
        itunes_page = await self.itunes_api.get_itunes_page(
            "music-video",
            alt_id,
        )
        return itunes_page["storePlatformData"]["product-dv"]["results"][alt_id]

    def get_m3u8_master_url_from_webplayback(self, webplayback: dict) -> str:
        m3u8_master_url = webplayback["hls-playlist-url"]
        return m3u8_master_url

    def get_m3u8_master_url_from_itunes_page_metadata(
        self,
        itunes_page_metadata: dict,
    ) -> dict:
        stream_url = itunes_page_metadata["offers"][0]["assets"][0]["hlsUrl"]

        url_parts = urllib.parse.urlparse(stream_url)
        query = urllib.parse.parse_qs(url_parts.query, keep_blank_values=True)
        query.update({"aec": "HD", "dsid": "1"})

        m3u8_master_url = url_parts._replace(
            query=urllib.parse.urlencode(query, doseq=True)
        ).geturl()

        return m3u8_master_url

    def get_alt_id(self, metadata: dict) -> str | None:
        music_video_url = metadata["attributes"].get("url")
        if music_video_url is None:
            return None

        alt_id = music_video_url.split("/")[-1].split("?")[0]
        logger.debug(f"Alt ID: {alt_id}")

        return alt_id

    @alru_cache()
    async def get_album(
        self,
        collection_id: int,
    ) -> dict | None:
        album_response = await self.apple_music_api.get_album(collection_id)
        if not album_response:
            return None
        return album_response["data"][0]

    async def get_tags(
        self,
        metadata: dict,
        itunes_page_metadata: dict,
    ) -> MediaTags:
        alt_id = self.get_alt_id(metadata)
        lookup_metadata = (await self.itunes_api.get_lookup_result(alt_id))["results"]

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
            date=self.parse_date(lookup_metadata[0]["releaseDate"]),
            genre=lookup_metadata[0]["primaryGenreName"],
            genre_id=int(itunes_page_metadata["genres"][0]["genreId"]),
            media_type=MediaType.MUSIC_VIDEO,
            storefront=int(self.itunes_api.storefront_id.split("-")[0]),
            title=lookup_metadata[0]["trackCensoredName"],
            title_id=int(metadata["id"]),
            rating=rating,
        )

        if len(lookup_metadata) > 1:
            album = await self.get_album(itunes_page_metadata["collectionId"])
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

        logger.debug(f"Tags: {tags}")

        return tags

    async def get_stream_info(
        self,
        metadata: dict,
        itunes_page_metadata: dict,
        codec_priority: list[MusicVideoCodec],
        resolution: MusicVideoResolution,
    ) -> StreamInfoAv:
        alt_video_id = self.get_alt_id(metadata)
        if alt_video_id == metadata["id"]:
            m3u8_master_url = self.get_m3u8_master_url_from_itunes_page_metadata(
                itunes_page_metadata,
            )
        else:
            webplayback_response = await self.apple_music_api.get_webplayback(
                metadata["id"]
            )
            m3u8_master_url = self.get_m3u8_master_url_from_webplayback(
                webplayback_response["songList"][0],
            )

        playlist_master_m3u8_obj = m3u8.loads(
            (await get_response(m3u8_master_url)).text
        )
        playlist_master_m3u8_obj.base_uri = m3u8_master_url.rpartition("/")[0]
        stream_info_video = await self.get_stream_info_video(
            playlist_master_m3u8_obj,
            codec_priority,
            resolution,
        )
        stream_info_audio = await self.get_stream_info_audio(
            playlist_master_m3u8_obj.data,
            codec_priority,
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
        logger.debug(f"Stream info: {stream_info}")

        return stream_info

    def get_video_playlist_from_resolution(
        self,
        video_playlists: list[m3u8.Playlist],
        codec_priority: list[MusicVideoCodec],
        resolution: MusicVideoResolution,
    ) -> m3u8.Playlist | None:
        playlist_results = []
        for codec_index, codec in enumerate(codec_priority):
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
            exceeds_resolution = playlist_resolution > int(resolution)
            resolution_difference = abs(playlist_resolution - int(resolution))

            return (
                exceeds_resolution,
                resolution_difference,
                codec_index,
                -playlist_resolution,
                -bandwidth,
            )

        playlist_results.sort(key=sort_key)
        return playlist_results[0][1]

    def get_best_stereo_audio_playlist(
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

    async def get_video_playlist_from_user(
        self,
        video_playlists: list[m3u8.Playlist],
    ) -> m3u8.Playlist:
        choices = [
            Choice(
                name=" | ".join(
                    [
                        playlist.stream_info.codecs[:4],
                        "x".join(str(v) for v in playlist.stream_info.resolution),
                        str(playlist.stream_info.bandwidth),
                    ]
                ),
                value=playlist,
            )
            for playlist in video_playlists
        ]
        selected = await inquirer.select(
            message="Select which video codec to download: (Codec | Resolution | Bitrate)",
            choices=choices,
        ).execute_async()

        return selected

    async def get_audio_playlist_from_user(
        self,
        playlist_master_data: dict,
    ) -> dict:
        choices = [
            Choice(
                name=playlist["group_id"],
                value=playlist,
            )
            for playlist in playlist_master_data["media"]
            if playlist.get("uri")
        ]
        selected = await inquirer.select(
            message="Select which audio codec to download:",
            choices=choices,
        ).execute_async()

        return selected

    def _get_key_by_format(
        self,
        m3u8_obj: m3u8.M3U8,
        key_format: str,
    ) -> str:
        return next(
            (key for key in m3u8_obj.keys if key.keyformat == key_format),
            None,
        ).uri

    def get_widevine_pssh(self, m3u8_obj: m3u8.M3U8) -> str:
        return self._get_key_by_format(
            m3u8_obj,
            "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",
        )

    def get_playready_pssh(self, m3u8_obj: m3u8.M3U8) -> str:
        return self._get_key_by_format(
            m3u8_obj,
            "com.microsoft.playready",
        )

    def get_fairplay_key(self, m3u8_obj: m3u8.M3U8) -> str:
        return self._get_key_by_format(
            m3u8_obj,
            "com.apple.streamingkeydelivery",
        )

    async def get_stream_info_video(
        self,
        playlist_master_m3u8_obj: m3u8.M3U8,
        codec_priority: list[MusicVideoCodec],
        resolution: MusicVideoResolution,
    ) -> StreamInfo | None:
        stream_info = StreamInfo()

        if MusicVideoCodec.ASK not in codec_priority:
            playlist = self.get_video_playlist_from_resolution(
                playlist_master_m3u8_obj.playlists,
                codec_priority,
                resolution,
            )
        else:
            playlist = await self.get_video_playlist_from_user(
                playlist_master_m3u8_obj.playlists
            )

        if not playlist:
            return None

        stream_info.stream_url = playlist.uri
        stream_info.codec = playlist.stream_info.codecs
        stream_info.width, stream_info.height = playlist.stream_info.resolution

        playlist_m3u8_obj = m3u8.loads(
            (await get_response(stream_info.stream_url)).text
        )
        stream_info.widevine_pssh = self.get_widevine_pssh(playlist_m3u8_obj)
        stream_info.fairplay_key = self.get_fairplay_key(playlist_m3u8_obj)
        stream_info.playready_pssh = self.get_playready_pssh(playlist_m3u8_obj)

        return stream_info

    async def get_stream_info_audio(
        self,
        playlist_master_data: dict,
        codec_priority: list[MusicVideoCodec],
    ) -> StreamInfo | None:
        stream_info = StreamInfo()

        if MusicVideoCodec.ASK not in codec_priority:
            playlist = self.get_best_stereo_audio_playlist(playlist_master_data)
        else:
            playlist = await self.get_audio_playlist_from_user(playlist_master_data)

        if not playlist:
            return None

        stream_info.stream_url = playlist["uri"]
        stream_info.codec = playlist["group_id"]

        playlist_m3u8_obj = m3u8.loads(
            (await get_response(stream_info.stream_url)).text
        )
        stream_info.widevine_pssh = self.get_widevine_pssh(playlist_m3u8_obj)
        stream_info.fairplay_key = self.get_fairplay_key(playlist_m3u8_obj)
        stream_info.playready_pssh = self.get_playready_pssh(playlist_m3u8_obj)

        return stream_info

    async def get_decryption_key(
        self,
        stream_info: StreamInfoAv,
        cdm: Cdm,
    ) -> DecryptionKeyAv:
        decryption_key_video = await AppleMusicInterface.get_decryption_key(
            self,
            stream_info.video_track.widevine_pssh,
            stream_info.media_id,
            cdm,
        )
        decryption_key_audio = await AppleMusicInterface.get_decryption_key(
            self,
            stream_info.audio_track.widevine_pssh,
            stream_info.media_id,
            cdm,
        )

        return DecryptionKeyAv(
            video_track=decryption_key_video,
            audio_track=decryption_key_audio,
        )
