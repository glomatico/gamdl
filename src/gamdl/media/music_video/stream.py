"""Music-video track discovery from an HLS master playlist."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from gamdl.api.exceptions import GamdlApiError

from ..enums import StreamOrigin
from ..exceptions import (
    SelectionRequiredError,
    StreamManifestError,
    StreamNotAvailableError,
)
from ..song.enums import SongCodec
from ..stream import AudioStream, StreamTransport, VideoStream
from .enums import MusicVideoCodec
from .models import (
    AudioSelectionOption,
    AudioSelectionRequest,
    MusicVideoStreamSelector,
    VideoSelectionOption,
    VideoSelectionRequest,
)

if TYPE_CHECKING:
    from m3u8.model import M3U8

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


def _audio_playlist_bitrate(manifest: M3U8) -> int | None:
    """Estimate audio bits/s from HLS segment hints or byte ranges.

    EXT-X-BITRATE is expressed in kbps. For byte-range playlists, segment
    byte counts and durations give an average without fetching media bytes.
    """
    total_bits = 0.0
    total_seconds = 0.0
    for segment in manifest.segments:
        duration = segment.duration
        if not duration or duration <= 0:
            continue
        hint = getattr(segment, "bitrate", None)
        if hint is not None and hint > 0:
            bits = hint * 1000 * duration
        elif segment.byterange:
            try:
                size = int(segment.byterange.split("@", 1)[0])
            except ValueError:
                continue
            if size <= 0:
                continue
            bits = size * 8
        else:
            continue
        total_bits += bits
        total_seconds += duration
    return round(total_bits / total_seconds) if total_seconds else None


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
        """Use iTunes HLS for normal videos and Apple web playback as fallback.

        Wrapper supplies account credentials and sometimes tagging metadata,
        but its playback endpoint does not offer music-video HLS streams.
        Catalog videos with a different ID in their canonical URL should use
        web playback first, matching GAMDL 3.x's behavior.
        """
        metadata = await self.media.get_metadata()
        attributes = metadata.get("attributes")
        url = attributes.get("url") if isinstance(attributes, dict) else None
        identifier = (
            urlsplit(url).path.rsplit("/", 1)[-1] if isinstance(url, str) else ""
        )
        catalog_id = (
            await self.media._catalog_id() if self.media.is_library else self.media.id
        )
        if not identifier.isdigit():
            identifier = catalog_id

        async def itunes_url() -> str | None:
            if self.itunes_api is None:
                return None
            try:
                page = await self.itunes_api.get_itunes_page("music-video", identifier)
            except GamdlApiError:
                return None
            return _itunes_hls_url(page, identifier)

        async def webplayback_url() -> str | None:
            try:
                playback = await self.media._load(
                    "music_video_webplayback",
                    lambda: self.media.api.get_webplayback(
                        self.media.id, is_library=self.media.is_library
                    ),
                )
            except GamdlApiError:
                return None
            entries = playback.get("songList") if isinstance(playback, dict) else None
            for entry in entries if isinstance(entries, list) else ():
                if isinstance(entry, dict):
                    hls_url = entry.get("hls-playlist-url")
                    if isinstance(hls_url, str) and hls_url:
                        return hls_url
            return None

        sources = (
            (
                (itunes_url, StreamOrigin.ITUNES_HLS),
                (webplayback_url, StreamOrigin.WEBPLAYBACK),
            )
            if identifier == catalog_id
            else (
                (webplayback_url, StreamOrigin.WEBPLAYBACK),
                (itunes_url, StreamOrigin.ITUNES_HLS),
            )
        )
        for fetch, origin in sources:
            if hls_url := await fetch():
                return hls_url, origin
        raise StreamNotAvailableError(
            "No music-video HLS stream available from iTunes or Apple Music web playback"
        )

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
        codec_priority: Sequence[MusicVideoCodec] | None = None,
        max_height: int | None = 1080,
        ask: bool = False,
        selector: MusicVideoStreamSelector | None = None,
    ) -> MusicVideoStreams:
        """Return an HLS video/audio pair with an optional two-stage selector."""
        if max_height is not None and (type(max_height) is not int or max_height <= 0):
            raise ValueError("max_height must be a positive integer or None")
        if video_codec is not None and codec_priority is not None:
            raise ValueError("Provide either video_codec or codec_priority, not both")
        if ask:
            if video_codec is not None or codec_priority is not None:
                raise ValueError("ask cannot be combined with codec preferences")
            if selector is None:
                raise SelectionRequiredError("music_video_streams")
            priorities: tuple[MusicVideoCodec, ...] = ()
        else:
            if selector is not None:
                raise ValueError("selector requires ask=True")
            try:
                if codec_priority is not None and isinstance(
                    codec_priority, (str, bytes)
                ):
                    raise ValueError(
                        "codec_priority must be a sequence of video codecs"
                    )
                priorities = (
                    (MusicVideoCodec(video_codec),)
                    if video_codec is not None
                    else tuple(MusicVideoCodec(codec) for codec in codec_priority)
                    if codec_priority is not None
                    else (MusicVideoCodec.H264, MusicVideoCodec.H265)
                )
            except (ValueError, TypeError):
                raise ValueError("video codecs must be h264 or h265") from None
            if not priorities:
                raise ValueError("codec_priority must not be empty")

        async def discover() -> MusicVideoStreams:
            master_url, origin = await self._master_url()
            manifest = await self.transport.get_manifest(master_url)
            choices = []
            for playlist in manifest.playlists:
                info = playlist.stream_info
                codec = _video_codec(getattr(info, "codecs", None))
                if codec is None or (not ask and codec not in priorities):
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
                # Codec order outweighs resolution/bitrate: try the best
                # eligible rendition of each codec before the next codec.
                # A video with no separate audio is not a usable A/V pair.
                ranked = sorted(
                    range(len(choices)),
                    key=lambda i: (
                        priorities.index(choices[i][1].codec),
                        -choices[i][1].height,
                        -(choices[i][1].bitrate or 0),
                    ),
                )
                index = next(
                    (
                        i
                        for i in ranked
                        if any(
                            rendition.type == "AUDIO"
                            and rendition.uri
                            and (
                                getattr(choices[i][0].stream_info, "audio", None)
                                is None
                                or rendition.group_id == choices[i][0].stream_info.audio
                            )
                            for rendition in manifest.media
                        )
                    ),
                    ranked[0],
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
            # An audio-only variant can advertise its own bandwidth. Video
            # variants advertise combined A/V bandwidth, which is NOT the
            # bitrate of the referenced EXT-X-MEDIA audio track.
            audio_bandwidths = {
                playlist.absolute_uri: getattr(
                    playlist.stream_info, "average_bandwidth", None
                )
                or getattr(playlist.stream_info, "bandwidth", None)
                for playlist in manifest.playlists
                if _video_codec(getattr(playlist.stream_info, "codecs", None)) is None
            }
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
                    bitrate=audio_bandwidths.get(audio.absolute_uri),
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

                async def populate_bitrate(option: AudioSelectionOption) -> None:
                    if option.bitrate:
                        return
                    try:
                        audio_manifest = await option.stream.get_manifest()
                    except StreamManifestError:
                        # Bitrate is optional metadata; a failed probe must not
                        # prevent choosing an otherwise valid audio rendition.
                        return
                    option.stream.bitrate = _audio_playlist_bitrate(audio_manifest)

                await asyncio.gather(
                    *(populate_bitrate(option) for option in audio_options)
                )
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
                priorities,
                max_height,
                id(self.wrapper_api),
                id(self.itunes_api),
                id(self.transport.client),
            ),
            discover,
        )
