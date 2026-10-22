"""Lazy song operations layered on the existing Apple Music API."""

from __future__ import annotations

from .base import Media
from .exceptions import GamdlMediaError
from .lyrics import Lyrics
from .tags import Tags


class Song(Media):
    """An Apple Music song; every network-dependent operation is explicit.

    Loading tags or cover metadata reuses the song's metadata. Requesting
    lyrics, assets or playback metadata never happens implicitly.
    """

    resource_type = "songs"
    library_type = "library-songs"

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
        """Derive common tags from cached catalog metadata, without disk IO."""

        async def load() -> Tags:
            return Tags.from_metadata(await self.get_metadata())

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
            return Lyrics.from_ttml(raw)

        return await self._load(key, load)

    async def get_assets(
        self,
        *,
        include_license_urls: bool = True,
        hls_encryption: str = "CBC",
        hls_profile: str = "enhancedHls",
    ) -> dict[str, Any]:
        """Fetch song asset metadata without downloading streams or licenses.

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

    async def get_webplayback(self) -> dict[str, Any]:
        """Fetch playback metadata only when requested; no DRM exchange."""
        return await self._load(
            "webplayback",
            lambda: self.api.get_webplayback(self.id, is_library=self.is_library),
        )
