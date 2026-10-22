"""Lazy song operations layered on the existing Apple Music API."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import httpx

from ..base import Media
from ..exceptions import GamdlMediaError
from .lyrics import Lyrics
from .stream import SongStreamResolver
from .models import StreamSelector
from ..stream import AudioStream
from .enums import SongCodec
from .tags import Tags

if TYPE_CHECKING:
    from ...api.wrapper import WrapperApi


class Song(Media):
    """An Apple Music song; every network-dependent operation is explicit.

    Cover metadata comes from the catalog resource; tags instead come from
    the first playback asset's metadata, as in Gamdl 3.x. Neither lyrics nor
    streaming assets are requested just because the song object was created.
    """

    resource_type = "songs"
    library_type = "library-songs"

    async def get_streams(
        self,
        *,
        wrapper_api: WrapperApi | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> tuple[AudioStream, ...]:
        """Discover every source, including both assets profiles, without media bytes."""
        return await SongStreamResolver(
            self, wrapper_api=wrapper_api, client=client
        ).get_streams()

    async def get_stream(
        self,
        *,
        codec: SongCodec | None = None,
        codec_priority: Sequence[SongCodec] | None = None,
        selector: StreamSelector | None = None,
        wrapper_api: WrapperApi | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> AudioStream:
        """Select a codec or ASK for a source first, then its advertised variant.

        With ASK the selector receives StreamSourceSelectionRequest followed by
        StreamSelectionRequest. Only the selected source is fetched.
        """
        return await SongStreamResolver(
            self, wrapper_api=wrapper_api, client=client
        ).get_stream(codec=codec, codec_priority=codec_priority, selector=selector)

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

    async def get_tags(self) -> Tags:
        """Derive tags from the first web-playback asset's metadata.

        Reuses a cached web-playback response if one has been requested. This
        intentionally does not fetch catalog metadata or lyrics, nor does it
        enrich missing fields from iTunes yet.

        Raises:
            GamdlMediaError: No playback asset contains usable metadata.
        """

        async def load() -> Tags:
            playback = await self._get_webplayback()
            song_list = playback.get("songList")
            if not isinstance(song_list, list) or not song_list:
                raise GamdlMediaError("Web playback returned no songs")
            first_song = song_list[0]
            if not isinstance(first_song, dict):
                raise GamdlMediaError("Web playback song has invalid metadata")
            assets = first_song.get("assets")
            if not isinstance(assets, list) or not assets:
                raise GamdlMediaError("Web playback returned no song assets")
            first_asset = assets[0]
            metadata = (
                first_asset.get("metadata") if isinstance(first_asset, dict) else None
            )
            if not isinstance(metadata, dict):
                raise GamdlMediaError("Web playback asset has no tag metadata")
            return Tags.from_asset_metadata(metadata)

        return await self._load("tags", load)

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
            catalog_id = await self._catalog_id()
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
