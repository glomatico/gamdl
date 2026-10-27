"""Catalog resource endpoints and ordered catalog relationship traversal."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any


class CatalogEndpoints:
    """Catalog reads, including playlist, album and artist relationships."""

    async def get_song(
        self,
        song_id: str,
        *,
        extend: str = "extendedAssetUrls",
        include: str = "lyrics,albums",
    ) -> dict[str, Any]:
        """Fetch one catalog song, optionally including lyrics and album data."""
        return await self._get(
            f"/v1/catalog/{self.storefront}/songs/{song_id}",
            operation="get_song",
            params={"extend": extend, "include": include},
        )

    async def get_lyrics(
        self,
        song_id: str,
        *,
        language: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a catalog song's standard lyrics resource.

        Uses the AMP ``/songs/{id}/lyrics`` endpoint rather than relying on
        the optional ``lyrics`` relationship included by :meth:`get_song`.
        The returned JSON usually contains TTML in
        ``data[0]["attributes"]["ttml"]``; the caller can decide how to
        parse or convert the document.

        Args:
            song_id: Catalog song identifier, not a personal-library ID.
            language: Optional Apple Music localization tag (``l`` parameter).

        Returns:
            Raw lyrics API response, including its ``data`` resources.

        Notes:
            Lyrics availability and access depend on the track, storefront,
            and account authorization. This is a private Apple Music endpoint
            and may change independently of the supported catalog API.
        """
        return await self._get(
            f"/v1/catalog/{self.storefront}/songs/{song_id}/lyrics",
            operation="get_lyrics",
            params={"l": language} if language is not None else None,
        )

    async def get_syllable_lyrics(
        self,
        song_id: str,
        *,
        language: str | None = None,
        lyrics_language: str | None = None,
        script: str | None = None,
        extend: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a catalog song's syllable- or word-timed lyrics resource.

        Uses the AMP ``/songs/{id}/syllable-lyrics`` endpoint. Apple may
        return TTML with timestamped ``<span>`` elements, supporting
        syllable-level highlighting. The response is returned unchanged;
        converting TTML to LRC/SRT belongs to a separate lyrics processor.

        Args:
            song_id: Catalog song identifier, not a personal-library ID.
            language: Optional general localization tag (``l``).
            lyrics_language: Optional lyrics-specific locale (``l[lyrics]``).
            script: Optional lyrics script preference (``l[script]``), such as
                ``"en-Latn"``.
            extend: Optional extension, e.g. ``"ttmlLocalizations"`` to
                request available translated/transliterated lyric metadata.

        Returns:
            Raw JSON response, typically with TTML at
            ``data[0]["attributes"]["ttml"]`` or localized lyric fields.

        Notes:
            Not all songs have syllable timing. This is a private endpoint;
            it may return an error for tracks without accessible lyrics.
        """
        params: dict[str, str] = {}
        if language is not None:
            params["l"] = language
        if lyrics_language is not None:
            params["l[lyrics]"] = lyrics_language
        if script is not None:
            params["l[script]"] = script
        if extend is not None:
            params["extend"] = extend

        return await self._get(
            f"/v1/catalog/{self.storefront}/songs/{song_id}/syllable-lyrics",
            operation="get_syllable_lyrics",
            params=params or None,
        )

    async def get_music_video(
        self, music_video_id: str, *, include: str = "albums"
    ) -> dict[str, Any]:
        """Fetch a catalog music video and its requested relationships."""
        return await self._get(
            f"/v1/catalog/{self.storefront}/music-videos/{music_video_id}",
            operation="get_music_video",
            params={"include": include},
        )

    async def get_uploaded_video(self, uploaded_video_id: str) -> dict[str, Any]:
        """Fetch metadata for an uploaded catalog video."""
        return await self._get(
            f"/v1/catalog/{self.storefront}/uploaded-videos/{uploaded_video_id}",
            operation="get_uploaded_video",
        )

    async def get_album(
        self, album_id: str, *, extend: str = "extendedAssetUrls"
    ) -> dict[str, Any]:
        """Fetch a catalog album with the requested extended asset metadata."""
        return await self._get(
            f"/v1/catalog/{self.storefront}/albums/{album_id}",
            operation="get_album",
            params={"extend": extend},
        )

    async def get_playlist(
        self,
        playlist_id: str,
        *,
        limit_tracks: int = 300,
        extend: str = "extendedAssetUrls",
    ) -> dict[str, Any]:
        """Fetch a catalog playlist with up to ``limit_tracks`` embedded tracks.

        For playlists spanning multiple pages, use :meth:`iter_playlist_tracks`
        rather than assuming the embedded track relationship is complete.
        """
        return await self._get(
            f"/v1/catalog/{self.storefront}/playlists/{playlist_id}",
            operation="get_playlist",
            params={"limit[tracks]": limit_tracks, "extend": extend},
        )

    async def get_artist(
        self,
        artist_id: str,
        *,
        include: str = "albums,music-videos",
        views: str = "full-albums,compilation-albums,live-albums,singles,top-songs",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Fetch an artist with selected relationships and views.

        ``include`` and ``views`` are comma-separated Apple Music names.
        ``limit`` applies to each requested relationship or view, not to the
        overall response. For a complete view, use :meth:`iter_artist_view`.
        """
        return await self._get(
            f"/v1/catalog/{self.storefront}/artists/{artist_id}",
            operation="get_artist",
            params={
                "include": include,
                "views": views,
                **{
                    f"limit[{section}]": limit
                    for section in [*include.split(","), *views.split(",")]
                },
            },
        )

    async def iter_playlist_tracks(
        self,
        playlist_id: str,
        *,
        is_library: bool = False,
        limit: int = 100,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield playlist tracks in order by following the ``tracks`` endpoint.

        Args:
            playlist_id: ID of the catalog or library playlist.
            is_library: Use the authenticated user's library endpoint instead
                of the storefront's catalog endpoint.
            limit: Requested page size, not a total track limit.

        Yields:
            Individual song or music-video resource dictionaries in playlist
            order, including items beyond the first page.
        """
        base = (
            f"/v1/me/library/playlists/{playlist_id}"
            if is_library
            else f"/v1/catalog/{self.storefront}/playlists/{playlist_id}"
        )
        page = await self._get(
            f"{base}/tracks",
            operation="iter_playlist_tracks",
            params={"limit": limit},
        )
        async for item in self._iterate_pages(page):
            yield item

    async def iter_album_tracks(
        self,
        album_id: str,
        *,
        is_library: bool = False,
        limit: int = 100,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield an album's tracks in order, following the ``tracks`` cursor.

        Args:
            album_id: Catalog or library album ID.
            is_library: Select the authenticated library album endpoint.
            limit: Requested page size, not a maximum number of tracks.

        Yields:
            Song and music-video resource dictionaries in the order returned
            by Apple Music.
        """
        base = (
            f"/v1/me/library/albums/{album_id}"
            if is_library
            else f"/v1/catalog/{self.storefront}/albums/{album_id}"
        )
        page = await self._get(
            f"{base}/tracks",
            operation="iter_album_tracks",
            params={"limit": limit},
        )
        async for item in self._iterate_pages(page):
            yield item

    async def iter_artist_view(
        self,
        artist_id: str,
        view: str,
        *,
        limit: int = 100,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield all resources in one catalog artist view.

        Args:
            artist_id: Catalog artist ID.
            view: Apple Music view name, such as ``full-albums``,
                ``singles``, or ``top-songs``.
            limit: Requested page size, not a total cap.

        Yields:
            Individual view resource dictionaries in server order.

        Raises:
            ValueError: If ``view`` contains path or query separators.
        """
        if not view or any(c in view for c in "/?#"):
            raise ValueError("Invalid artist view")
        page = await self._get(
            f"/v1/catalog/{self.storefront}/artists/{artist_id}/view/{view}",
            operation="iter_artist_view",
            params={"limit": limit},
        )
        async for item in self._iterate_pages(page):
            yield item

    async def iter_artist_relationship(
        self,
        artist_id: str,
        relationship: str,
        *,
        limit: int = 100,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream all artist albums or music videos across API pages."""
        if relationship not in {"albums", "music-videos"}:
            raise ValueError("Unsupported artist relationship")
        page = await self._get(
            f"/v1/catalog/{self.storefront}/artists/{artist_id}/{relationship}",
            operation="iter_artist_relationship",
            params={"limit": limit},
        )
        async for item in self._iterate_pages(page):
            yield item
