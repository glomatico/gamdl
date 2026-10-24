"""Music-video track discovery from an HLS master playlist."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from gamdl.api.exceptions import GamdlApiError

from ..enums import StreamOrigin
from ..exceptions import StreamNotAvailableError
from ..stream import AudioStream, StreamTransport, VideoStream

if TYPE_CHECKING:
    from gamdl.api import ItunesApi
    from gamdl.api.wrapper import WrapperApi
    from .media import MusicVideo


@dataclass(frozen=True)
class MusicVideoStreams:
    """Separate audio and video tracks from one video HLS master."""

    video: VideoStream
    audio: AudioStream


def _video_codec(codecs: str | None) -> str | None:
    if not isinstance(codecs, str):
        return None
    for part in codecs.split(","):
        part = part.strip().lower()
        if part.startswith(("avc1.", "avc3.")):
            return "h264"
        if part.startswith(("hvc1.", "hev1.", "dvh1.", "dvhe.")):
            return "h265"
        if part.startswith("av01."):
            return "av1"
    return None


def _itunes_hls_url(page: dict[str, Any], media_id: str) -> str | None:
    product = (
        page.get("storePlatformData", {})
        .get("product-dv", {})
        .get("results", {})
        .get(media_id, {})
    )
    if not isinstance(product, dict):
        return None
    offers = product.get("offers")
    if not isinstance(offers, list):
        return None
    for offer in offers:
        assets = offer.get("assets") if isinstance(offer, dict) else None
        if not isinstance(assets, list):
            continue
        for asset in assets:
            url = asset.get("hlsUrl") if isinstance(asset, dict) else None
            if isinstance(url, str) and url:
                # Match Gamdl 3's iTunes video URL normalization.
                url = url.replace("play-edge.itunes.apple.com", "play.itunes.apple.com")
                url = url.replace("MZPlayLocal.woa", "MZPlay.woa")
                parsed = urlsplit(url)
                query = dict(parse_qsl(parsed.query, keep_blank_values=True))
                query.update({"aec": "HD", "dsid": "1"})
                return urlunsplit(parsed._replace(query=urlencode(query)))
    return None


class MusicVideoStreamResolver:
    """Choose an audio/video pair from one HLS master, without downloading media."""

    def __init__(
        self,
        media: MusicVideo,
        *,
        wrapper_api: WrapperApi | None = None,
        itunes_api: ItunesApi | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.media = media
        self.wrapper_api = wrapper_api
        self.itunes_api = itunes_api
        self.transport = StreamTransport(client)

    async def _master_url(self) -> tuple[str, StreamOrigin]:
        if self.wrapper_api is not None and not self.media.is_library:
            try:
                playback = await self.media._get_wrapper_playback(self.wrapper_api)
            except GamdlApiError:
                playback = {}
            songs = playback.get("songList")
            if isinstance(songs, list) and songs and isinstance(songs[0], dict):
                url = songs[0].get("hls-playlist-url")
                if isinstance(url, str) and url:
                    return url, StreamOrigin.WRAPPER_HLS

        if self.itunes_api is None:
            raise StreamNotAvailableError("Video HLS requires wrapper or iTunes API")
        metadata = await self.media.get_metadata()
        attributes = metadata.get("attributes")
        url = attributes.get("url") if isinstance(attributes, dict) else None
        identifier = (
            urlsplit(url).path.rsplit("/", 1)[-1] if isinstance(url, str) else ""
        )
        if not identifier.isdigit():
            identifier = (
                await self.media._catalog_id()
                if self.media.is_library
                else self.media.id
            )
        page = await self.itunes_api.get_itunes_page("music-video", identifier)
        hls_url = _itunes_hls_url(page, identifier)
        if hls_url is None:
            raise StreamNotAvailableError("No HLS URL in music-video iTunes metadata")
        return hls_url, StreamOrigin.ITUNES_HLS

    async def get_streams(
        self,
        *,
        video_codec: str = "h264",
        max_height: int = 1080,
    ) -> MusicVideoStreams:
        if video_codec not in {"h264", "h265", "av1"}:
            raise ValueError("video_codec must be h264, h265, or av1")
        if max_height <= 0:
            raise ValueError("max_height must be positive")

        async def discover() -> MusicVideoStreams:
            master_url, origin = await self._master_url()
            manifest = await self.transport.get_manifest(master_url)
            choices = []
            for playlist in manifest.playlists:
                info = playlist.stream_info
                codec_string = getattr(info, "codecs", None)
                if _video_codec(codec_string) != video_codec:
                    continue
                resolution = getattr(info, "resolution", None)
                if not resolution or len(resolution) != 2:
                    continue
                width, height = resolution
                if not width or not height:
                    continue
                bandwidth = getattr(info, "average_bandwidth", None) or getattr(
                    info, "bandwidth", None
                )
                choices.append((playlist, width, height, bandwidth or 0))

            if not choices:
                raise StreamNotAvailableError("No matching music-video HLS rendition")
            # Choose the highest rendition within the requested ceiling;
            # otherwise the smallest available rendition above it.
            eligible = [c for c in choices if c[2] <= max_height]
            video_playlist, width, height, bitrate = (
                max(eligible, key=lambda c: (c[2], c[3]))
                if eligible
                else min(choices, key=lambda c: (c[2], -c[3]))
            )
            video_info = video_playlist.stream_info
            video_group = getattr(video_info, "audio", None)
            renditions = [
                audio
                for audio in manifest.media
                if audio.type == "AUDIO"
                and audio.uri
                and (video_group is None or audio.group_id == video_group)
            ]
            if not renditions:
                raise StreamNotAvailableError(
                    "Video rendition has no separate audio HLS track"
                )
            # Prefer stereo/default renditions if the master exposes multiple.
            audio = sorted(
                renditions,
                key=lambda a: (
                    str(getattr(a, "default", "")).upper() not in {"YES", "TRUE"},
                    "stereo" not in str(a.group_id).lower(),
                    str(a.name),
                ),
            )[0]
            from ..song.enums import SongCodec

            audio_codec_value = str(video_info.codecs or "").lower()
            audio_group = str(audio.group_id).lower()
            if "ec-3" in audio_codec_value or "atmos" in audio_group:
                audio_codec = SongCodec.ATMOS
            elif "ac-3" in audio_codec_value:
                audio_codec = SongCodec.AC3
            elif "mp4a.40.5" in audio_codec_value:
                audio_codec = SongCodec.AAC_HE
            else:
                audio_codec = SongCodec.AAC

            return MusicVideoStreams(
                video=VideoStream(
                    codec=video_codec,
                    codec_string=video_info.codecs,
                    width=width,
                    height=height,
                    frame_rate=getattr(video_info, "frame_rate", None),
                    bitrate=bitrate,
                    origin=origin,
                    url=video_playlist.absolute_uri,
                    transport=self.transport,
                    media_id=self.media.id,
                ),
                audio=AudioStream(
                    codec=audio_codec,
                    codec_string=audio_codec_value,
                    origin=origin,
                    url=audio.absolute_uri,
                    transport=self.transport,
                    media_id=self.media.id,
                ),
            )

        return await self.media._load(
            (
                "video_streams",
                video_codec,
                max_height,
                id(self.wrapper_api),
                id(self.itunes_api),
                id(self.transport.client),
            ),
            discover,
        )
