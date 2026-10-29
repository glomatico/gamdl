"""Lazy song operations layered on the existing Apple Music API."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import httpx

from gamdl.api.exceptions import GamdlApiResponseError

from ..base import Media
from ..exceptions import GamdlMediaError, StreamNotAvailableError
from ..stream import AudioStream, HttpStream
from .enums import SongCodec
from .lyrics import Lyrics
from .models import StreamSelector
from .stream import SongStreamResolver

if TYPE_CHECKING:
    from ...api.wrapper import WrapperApi


class Song(Media):
    """An Apple Music song; every network-dependent operation is explicit.

    Cover metadata comes from the catalog resource; playback metadata is
    available independently for downstream tag resolution. Neither lyrics nor
    streaming assets are requested just because the song object was created.
    """

    resource_type = "songs"
    library_type = "library-songs"

    async def is_uploaded(self) -> bool:
        """Whether a personal-library song has no catalog counterpart."""
        if not self.is_library:
            return False
        try:
            await self._catalog_id()
        except GamdlMediaError:
            return True
        return False

    async def get_uploaded_stream(self) -> tuple[HttpStream, str]:
        """Get the sole direct webplayback file for an unmatched library song."""
        if not await self.is_uploaded():
            raise StreamNotAvailableError("Song is not an unmatched library upload")

        async def load() -> tuple[HttpStream, str]:
            payload = await self._get_webplayback()
            songs = payload.get("songList") or []
            assets = songs[0].get("assets") or [] if songs else []
            if len(assets) != 1 or not isinstance(assets[0], dict):
                raise StreamNotAvailableError(
                    "Uploaded song has no single direct asset"
                )
            asset = assets[0]
            url = asset.get("URL")
            extension = (asset.get("metadata") or {}).get("fileExtension")
            if not isinstance(url, str) or not url.startswith("https://"):
                raise StreamNotAvailableError("Uploaded song has no HTTPS asset")
            if not isinstance(extension, str) or extension.lower() not in {
                "mp3",
                "m4a",
                "mp4",
                "aac",
            }:
                raise StreamNotAvailableError("Unsupported uploaded song format")
            return HttpStream(url, media_id=self.id), "." + extension.lower()

        return await self._load("uploaded_stream", load)

    async def get_streams(
        self,
        *,
        wrapper_api: WrapperApi | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> tuple[AudioStream | HttpStream, ...]:
        """Discover every source, including both assets profiles, without media bytes."""
        if await self.is_uploaded():
            stream, _ = await self.get_uploaded_stream()
            return (stream,)
        return await SongStreamResolver(
            self, wrapper_api=wrapper_api, client=client
        ).get_streams()

    async def get_stream(
        self,
        *,
        codec: SongCodec | None = None,
        codec_priority: Sequence[SongCodec] | None = None,
        ask: bool = False,
        selector: StreamSelector | None = None,
        wrapper_api: WrapperApi | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> AudioStream | HttpStream:
        """Select a preferred codec, or interactively choose source and variant.

        With ``ask=True`` the selector receives a StreamSourceSelectionRequest
        followed by StreamSelectionRequest. Only the chosen source is fetched;
        codec preferences cannot override an interactive choice.
        """
        if await self.is_uploaded():
            stream, _ = await self.get_uploaded_stream()
            return stream
        return await SongStreamResolver(
            self, wrapper_api=wrapper_api, client=client
        ).get_stream(
            codec=codec, codec_priority=codec_priority, ask=ask, selector=selector
        )

    async def _fetch_metadata(self) -> dict[str, Any]:
        payload = (
            await self.api.get_library_song(self.id)
            if self.is_library
            else await self.api.get_song(self.id)
        )
        items = payload.get("data")
        if not isinstance(items, list) or not items:
            raise GamdlMediaError("Apple Music returned no song metadata")
        return items[0]

    async def get_lyrics(
        self,
        *,
        syllable: bool = False,
        language: str | None = None,
        lyrics_language: str | None = None,
        script: str | None = None,
    ) -> Lyrics | None:
        """Fetch, parse and cache standard or syllable-timed TTML on demand.

        This returns ``None`` if the API returns no TTML resource; API failures
        still propagate. Locale and timing options have distinct cache keys.
        """
        key = ("lyrics", syllable, language, lyrics_language, script)

        async def load() -> Lyrics | None:
            # Interludes and instrumental tracks explicitly advertise that
            # lyrics do not exist. Apple returns HTTP 404 for their lyrics
            # endpoint, which must not fail an otherwise successful download.
            metadata = await self.get_metadata()
            if metadata.get("attributes", {}).get("hasLyrics") is False:
                return None
            catalog_id = await self._catalog_id()
            try:
                if syllable:
                    payload = await self.api.get_syllable_lyrics(
                        catalog_id,
                        language=language,
                        lyrics_language=lyrics_language,
                        script=script,
                        extend="ttmlLocalizations",
                    )
                else:
                    payload = await self.api.get_lyrics(catalog_id, language=language)
            except GamdlApiResponseError as exc:
                # Some catalog resources omit `hasLyrics`, while the endpoint
                # still returns 404 for unavailable lyrics. Do not suppress
                # authorization, rate limits, transport or other API errors.
                if exc.status_code != 404:
                    raise
                return None
            items = payload.get("data") or []
            if not items:
                return None
            attrs = items[0].get("attributes", {})
            raw = attrs.get("ttml")
            if not isinstance(raw, str):
                localizations = attrs.get("ttmlLocalizations")
                if isinstance(localizations, str):
                    raw = localizations
                elif isinstance(localizations, dict):
                    raw = next(
                        (v for v in localizations.values() if isinstance(v, str)), None
                    )
            if not isinstance(raw, str) or not raw:
                return None
            return Lyrics.from_ttml(raw, syllable=syllable)

        return await self._load(key, load)

    async def _get_assets(
        self,
        *,
        include_license_urls: bool = True,
        hls_encryption: str = "CBC",
        hls_profile: str = "enhancedHls",
    ) -> dict[str, Any]:
        """Internal: fetch song asset metadata without downloading media.

        Asset preferences are part of the cache key. The stream/decryption
        abstraction is a later milestone; this returns raw asset metadata.
        """
        key = ("assets", include_license_urls, hls_encryption, hls_profile)

        async def load() -> dict[str, Any]:
            return await self.api.get_assets(
                await self._catalog_id(),
                kind="song",
                include_license_urls=include_license_urls,
                hls_encryption=hls_encryption,
                hls_profile=hls_profile,
            )

        return await self._load(key, load)

    async def _get_webplayback(self) -> dict[str, Any]:
        """Internal: lazily cache web playback for tagging and future streams."""
        return await self._load(
            "webplayback",
            lambda: self.api.get_webplayback(self.id, is_library=self.is_library),
        )
