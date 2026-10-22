"""Apple Music personal-library resource and collection endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any


class LibraryEndpoints:
    """Authenticated library resource operations and lazy iterators."""

    async def get_library_song(
        self,
        song_id: str,
        *,
        include: str = "catalog",
        extend: str = "extendedAssetUrls",
    ) -> dict[str, Any]:
        """Fetch one user's library song, optionally including its catalog item."""
        return await self._get(
            f"/v1/me/library/songs/{song_id}",
            operation="get_library_song",
            params={"include": include, "extend": extend},
        )

    async def get_library_music_video(
        self, music_video_id: str, *, include: str = "catalog"
    ) -> dict[str, Any]:
        """Fetch one user's library music video and requested relationships."""
        return await self._get(
            f"/v1/me/library/music-videos/{music_video_id}",
            operation="get_library_music_video",
            params={"include": include},
        )

    async def get_library_album(
        self,
        album_id: str,
        *,
        include: str = "catalog",
        extend: str = "extendedAssetUrls",
    ) -> dict[str, Any]:
        """Fetch one user's library album and requested relationships."""
        return await self._get(
            f"/v1/me/library/albums/{album_id}",
            operation="get_library_album",
            params={"include": include, "extend": extend},
        )

    async def get_library_playlist(
        self,
        playlist_id: str,
        *,
        include: str = "catalog,tracks",
        limit: int = 100,
        extend: str = "extendedAssetUrls",
    ) -> dict[str, Any]:
        """Fetch one library playlist and its selected included resources.

        ``limit`` applies to each resource type named in ``include``. For all
        of the playlist's tracks, use :meth:`iter_playlist_tracks` with
        ``is_library=True``.
        """
        return await self._get(
            f"/v1/me/library/playlists/{playlist_id}",
            operation="get_library_playlist",
            params={
                "include": include,
                **{f"limit[{section}]": limit for section in include.split(",")},
                "extend": extend,
            },
        )

    async def _library_list(
        self,
        type_name: str,
        *,
        limit: int,
        offset: int,
        include: str,
        extend: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one page of a user-library collection."""
        params: dict[str, Any] = {"limit": limit, "offset": offset, "include": include}
        if extend is not None:
            params["extend"] = extend
        return await self._get(
            f"/v1/me/library/{type_name}",
            operation=f"get_library_{type_name.replace('-', '_')}",
            params=params,
        )

    async def get_library_songs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        include: str = "catalog",
        extend: str = "extendedAssetUrls",
    ) -> dict[str, Any]:
        """Fetch one page of library songs; ``limit`` is the page size."""
        return await self._library_list(
            "songs", limit=limit, offset=offset, include=include, extend=extend
        )

    async def get_library_music_videos(
        self, *, limit: int = 100, offset: int = 0, include: str = "catalog"
    ) -> dict[str, Any]:
        """Fetch one page of library music videos; ``offset`` skips prior items."""
        return await self._library_list(
            "music-videos", limit=limit, offset=offset, include=include
        )

    async def get_library_albums(
        self, *, limit: int = 100, offset: int = 0, include: str = "catalog"
    ) -> dict[str, Any]:
        """Fetch one page of library albums without following ``next``."""
        return await self._library_list(
            "albums", limit=limit, offset=offset, include=include
        )

    async def get_library_playlists(
        self, *, limit: int = 100, offset: int = 0, include: str = "catalog"
    ) -> dict[str, Any]:
        """Fetch one page of library playlists without following ``next``."""
        return await self._library_list(
            "playlists", limit=limit, offset=offset, include=include
        )

    async def iter_library_playlists(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        include: str = "catalog",
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield library playlists in server order, across all pages.

        Args:
            limit: Requested number of playlists per page, not a total cap.
            offset: Number of items to skip before the first page.
            include: Comma-separated relationships to request.

        Yields:
            Individual Apple Music library-playlist resource dictionaries.
        """
        page = await self.get_library_playlists(
            limit=limit, offset=offset, include=include
        )
        async for item in self._iterate_pages(page):
            yield item

    async def iter_library_songs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        include: str = "catalog",
        extend: str = "extendedAssetUrls",
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield library songs lazily, following subsequent pages in order.

        ``limit`` determines page size and ``offset`` the starting position.
        ``include`` and ``extend`` are forwarded to the initial song query.

        Yields:
            Individual library-song resource dictionaries.
        """
        page = await self.get_library_songs(
            limit=limit, offset=offset, include=include, extend=extend
        )
        async for item in self._iterate_pages(page):
            yield item

    async def iter_library_music_videos(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        include: str = "catalog",
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield all library music videos, starting at ``offset``.

        ``limit`` is the requested page size; pagination is automatic.
        Each yielded value is a library music-video resource dictionary.
        """
        page = await self.get_library_music_videos(
            limit=limit, offset=offset, include=include
        )
        async for item in self._iterate_pages(page):
            yield item

    async def iter_library_albums(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        include: str = "catalog",
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield all library albums in server order, starting at ``offset``.

        ``limit`` specifies the requested page size, not a result limit.
        Each yielded value is a library-album resource dictionary.
        """
        page = await self.get_library_albums(
            limit=limit, offset=offset, include=include
        )
        async for item in self._iterate_pages(page):
            yield item
