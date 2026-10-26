"""Music-video track discovery from an HLS master playlist."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from gamdl.api.exceptions import GamdlApiError

from ..enums import StreamOrigin
from ..exceptions import SelectionRequiredError, StreamNotAvailableError
from ..stream import AudioStream, StreamTransport, VideoStream
from ..song.enums import SongCodec
from .enums import MusicVideoCodec
from .models import (
    AudioSelectionOption,
    AudioSelectionRequest,
    MusicVideoStreamSelector,
    VideoSelectionOption,
    VideoSelectionRequest,
)

if TYPE_CHECKING:
    from gamdl.api import ItunesApi
    from gamdl.api.wrapper import WrapperApi
    from .media import MusicVideo


@dataclass(frozen=True)
class MusicVideoStreams:
    """Separate audio and video tracks from one video HLS master."""

    video: VideoStream
    audio: AudioStream


def _video_codec(codecs: str | None) -> MusicVideoCodec | None:
    if not isinstance(codecs, str):
        return None
    for part in codecs.split(","):
        part = part.strip().lower()
        if part.startswith(("avc1.", "avc3.")):
            return MusicVideoCodec.H264
        if part.startswith(("hvc1.", "hev1.", "dvh1.", "dvhe.")):
            return MusicVideoCodec.H265
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

    @staticmethod
    async def _select_index(
        selector: MusicVideoStreamSelector,
        request: VideoSelectionRequest | AudioSelectionRequest,
        count: int,
    ) -> int:
        selected = selector(request)
        if inspect.isawaitable(selected):
            selected = await selected
        if type(selected) is not int or not 0 <= selected < count:
            raise ValueError("Music-video selector returned an invalid index")
        return selected

    async def get_streams(
        self,
        *,
        video_codec: MusicVideoCodec | None = None,
        max_height: int | None = 1080,
        ask: bool = False,
        selector: MusicVideoStreamSelector | None = None,
    ) -> MusicVideoStreams:
        """Return an HLS video/audio pair with an optional two-stage selector."""
        if max_height is not None and (type(max_height) is not int or max_height <= 0):
            raise ValueError("max_height must be a positive integer or None")
        if ask:
            if video_codec is not None:
                raise ValueError("ask cannot be combined with video_codec")
            if selector is None:
                raise SelectionRequiredError("music_video_streams")
        else:
            if selector is not None:
                raise ValueError("selector requires ask=True")
            try:
                video_codec = MusicVideoCodec(
                    video_codec if video_codec is not None else MusicVideoCodec.H264
                )
            except ValueError:
                raise ValueError("video_codec must be h264 or h265") from None

        async def discover() -> MusicVideoStreams:
            master_url, origin = await self._master_url()
            manifest = await self.transport.get_manifest(master_url)
            choices = []
            for playlist in manifest.playlists:
                info = playlist.stream_info
                codec = _video_codec(getattr(info, "codecs", None))
                if codec is None or (not ask and codec != video_codec):
                    continue
                resolution = getattr(info, "resolution", None)
                if not resolution or len(resolution) != 2:
                    continue
                width, height = resolution
                if (
                    not width
                    or not height
                    or (max_height is not None and height > max_height)
                ):
                    continue
                bandwidth = getattr(info, "average_bandwidth", None) or getattr(
                    info, "bandwidth", None
                )
                video_stream = VideoStream(
                    codec=codec,
                    codec_string=info.codecs,
                    width=width,
                    height=height,
                    frame_rate=getattr(info, "frame_rate", None),
                    bitrate=bandwidth or 0,
                    origin=origin,
                    url=playlist.absolute_uri,
                    transport=self.transport,
                    media_id=self.media.id,
                )
                choices.append((playlist, video_stream))

            if not choices:
                raise StreamNotAvailableError(
                    "No music-video HLS rendition matches the codec and height limit"
                )
            if ask:
                assert selector is not None
                request = VideoSelectionRequest(
                    media_id=self.media.id,
                    options=tuple(
                        VideoSelectionOption(i, stream)
                        for i, (_, stream) in enumerate(choices)
                    ),
                )
                index = await self._select_index(selector, request, len(choices))
            else:
                index = max(
                    range(len(choices)),
                    key=lambda i: (choices[i][1].height, choices[i][1].bitrate or 0),
                )
            video_playlist, video_stream = choices[index]
            video_info = video_playlist.stream_info
            video_group = getattr(video_info, "audio", None)
            # Separate A/V tracks can be chosen independently. Audio groups
            # from other variants in the same master may offer different
            # codecs (such as AAC versus Atmos), as in GAMDL 3.x.
            group_codecs = {
                group: str(getattr(playlist.stream_info, "codecs", "") or "").lower()
                for playlist in manifest.playlists
                if _video_codec(getattr(playlist.stream_info, "codecs", None))
                is not None
                if (group := getattr(playlist.stream_info, "audio", None))
            }
            renditions = [
                audio
                for audio in manifest.media
                if audio.type == "AUDIO"
                and audio.uri
                and (
                    audio.group_id in group_codecs
                    if ask and group_codecs
                    else video_group is None or audio.group_id == video_group
                )
            ]
            if not renditions:
                raise StreamNotAvailableError(
                    "Video rendition has no separate audio HLS track"
                )

            audio_options = []
            for audio in renditions:
                audio_group = str(audio.group_id).lower()
                audio_codec_value = group_codecs.get(
                    audio.group_id, str(video_info.codecs or "").lower()
                )
                if "ec-3" in audio_codec_value or "atmos" in audio_group:
                    audio_codec = SongCodec.ATMOS
                elif "ac-3" in audio_codec_value:
                    audio_codec = SongCodec.AC3
                elif "mp4a.40.5" in audio_codec_value:
                    audio_codec = SongCodec.AAC_HE
                else:
                    audio_codec = SongCodec.AAC
                audio_stream = AudioStream(
                    codec=audio_codec,
                    codec_string=audio_codec_value,
                    origin=origin,
                    url=audio.absolute_uri,
                    transport=self.transport,
                    media_id=self.media.id,
                )
                audio_options.append(
                    AudioSelectionOption(
                        index=len(audio_options),
                        stream=audio_stream,
                        group_id=audio.group_id,
                        name=audio.name,
                        language=getattr(audio, "language", None),
                    )
                )
            if ask:
                assert selector is not None
                audio_request = AudioSelectionRequest(
                    media_id=self.media.id,
                    video=video_stream,
                    options=tuple(audio_options),
                )
                selected = await self._select_index(
                    selector, audio_request, len(audio_options)
                )
            else:
                selected = min(
                    range(len(renditions)),
                    key=lambda i: (
                        str(getattr(renditions[i], "default", "")).upper()
                        not in {"YES", "TRUE"},
                        "stereo" not in str(renditions[i].group_id).lower(),
                        str(renditions[i].name),
                    ),
                )
            return MusicVideoStreams(
                video=video_stream, audio=audio_options[selected].stream
            )

        if ask:
            # Interactive decisions are intentionally not memoized: the next
            # invocation must give the user a fresh opportunity to choose.
            return await discover()
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
