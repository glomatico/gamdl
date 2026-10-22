"""Lazy, shared functionality for individual Apple Music media resources."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from .enums import DrmBackend
from .exceptions import GamdlMediaError, UnsupportedMediaError
from .models import MediaSource

if TYPE_CHECKING:
    from gamdl.api import AppleMusicApi

    from .cover import Cover
    from .stream import AudioStream
    from .drm import ContentKey

T = TypeVar("T")


def _first_resource(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract one resource consistently for supported video media models."""
    values = payload.get("data")
    if not isinstance(values, list) or not values:
        raise GamdlMediaError("Apple Music returned no media metadata")
    return values[0]


class Media:
    """A single media resource with optional prefetched metadata.

    Constructing an object never performs IO. Lazy operations are cached per
    instance and coalesced while in flight. The Apple Music API is caller-owned.
    """

    resource_type: str = ""
    library_type: str = ""

    def __init__(
        self,
        media_id: str,
        *,
        api: AppleMusicApi,
        is_library: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not media_id:
            raise ValueError("A media ID must not be empty")
        self.id = media_id
        self.api = api
        self.is_library = is_library
        self.source: MediaSource | None = None
        self._cache: dict[object, Any] = {}
        self._inflight: dict[object, asyncio.Task[Any]] = {}
        if metadata is not None:
            self._cache["metadata"] = metadata

    @property
    def type(self) -> str:
        return self.library_type if self.is_library else self.resource_type

    @classmethod
    def from_id(
        cls,
        media_id: str,
        *,
        api: AppleMusicApi,
        media_type: str | None = None,
        is_library: bool = False,
    ) -> Media:
        """Create an unloaded object; generic Media requires ``media_type``."""
        if cls is Media:
            target, inferred_library = cls._target_for_type(media_type)
            return target(media_id, api=api, is_library=is_library or inferred_library)
        if media_type is not None and media_type not in {
            cls.resource_type,
            cls.library_type,
        }:
            raise UnsupportedMediaError("Media type does not match the requested class")
        return cls(
            media_id, api=api, is_library=is_library or media_type == cls.library_type
        )

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any], *, api: AppleMusicApi) -> Media:
        """Instantiate and preseed metadata so the first read makes no request."""
        if not isinstance(metadata, dict) or not isinstance(metadata.get("id"), str):
            raise GamdlMediaError("Expected a media resource containing an ID")
        kind = metadata.get("type")
        target, is_library = cls._target_for_type(kind)
        if cls is not Media and target is not cls:
            raise UnsupportedMediaError(
                "Media metadata does not match the requested class"
            )
        return target(metadata["id"], api=api, is_library=is_library, metadata=metadata)

    @staticmethod
    def _target_for_type(media_type: str | None) -> tuple[type[Media], bool]:
        from .song import Song
        from .music_video import MusicVideo
        from .uploaded_video import UploadedVideo

        targets: dict[str, tuple[type[Media], bool]] = {
            "song": (Song, False),
            "songs": (Song, False),
            "library-songs": (Song, True),
            "music-video": (MusicVideo, False),
            "music-videos": (MusicVideo, False),
            "library-music-videos": (MusicVideo, True),
            "uploaded-video": (UploadedVideo, False),
            "uploaded-videos": (UploadedVideo, False),
        }
        result = targets.get(media_type) if media_type is not None else None
        if result is None:
            raise UnsupportedMediaError(f"Unsupported media type: {media_type!r}")
        return result

    async def _load(self, key: object, factory: Callable[[], Awaitable[T]]) -> T:
        """Cache successes, share simultaneous loads, and permit retries.

        Shielding prevents one canceled waiter from canceling a shared load.
        Completion callbacks clear failures and avoid unobserved task errors.
        """
        if key in self._cache:
            return self._cache[key]
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(factory())
            self._inflight[key] = task

            def finish(done: asyncio.Task[Any]) -> None:
                try:
                    value = done.result()
                except BaseException:
                    value = None
                    success = False
                else:
                    success = True
                if self._inflight.get(key) is done:
                    self._inflight.pop(key, None)
                    if success:
                        self._cache[key] = value

            task.add_done_callback(finish)
        return await asyncio.shield(task)

    def invalidate(self) -> None:
        """Clear cached values; any already-running calls may still complete."""
        self._cache.clear()
        self._inflight.clear()

    async def _fetch_metadata(self) -> dict[str, Any]:
        raise NotImplementedError

    async def get_metadata(self) -> dict[str, Any]:
        """Fetch resource metadata once, or use data supplied by from_metadata."""
        return await self._load("metadata", self._fetch_metadata)

    async def _catalog_id(self) -> str:
        """Find a catalog equivalent for library media when one exists."""
        if not self.is_library:
            return self.id

        async def load() -> str:
            metadata = await self.get_metadata()
            relationship = metadata.get("relationships", {}).get("catalog")
            # Prefetched library tracks may omit optional relationships. Only
            # re-fetch when the entire relationship was absent, not when Apple
            # explicitly provided an empty catalog-equivalent collection.
            if relationship is None:
                if self.resource_type == "songs":
                    response = await self.api.get_library_song(self.id)
                elif self.resource_type == "music-videos":
                    response = await self.api.get_library_music_video(self.id)
                else:
                    raise GamdlMediaError("Unsupported library catalog lookup")
                resources = response.get("data") or []
                relationship = (
                    resources[0].get("relationships", {}).get("catalog")
                    if resources
                    else None
                )
            entries = (
                relationship.get("data", []) if isinstance(relationship, dict) else []
            )
            if entries and isinstance(entries[0].get("id"), str):
                return entries[0]["id"]
            raise GamdlMediaError("This library resource has no catalog equivalent")

        return await self._load("catalog_id", load)

    async def get_cover(self) -> Cover | None:
        """Return artwork URL metadata, without fetching image bytes."""
        from .cover import Cover

        async def load() -> Cover | None:
            artwork = (await self.get_metadata()).get("attributes", {}).get("artwork")
            return Cover.from_metadata(artwork) if isinstance(artwork, dict) else None

        return await self._load("cover", load)

    async def get_keys(
        self,
        stream: AudioStream,
        *,
        backend: DrmBackend | str = DrmBackend.WIDEVINE,
        device_path: str | Path | None = None,
        cdm: Any | None = None,
    ) -> tuple[ContentKey, ...]:
        """Get authorized content keys for this media's selected stream.

        Requires a caller-supplied, legitimately provisioned CDM or device
        path for protected content. This never caches licenses or keys.
        Unprotected streams return an empty tuple without making requests.
        """
        from .drm import get_stream_keys
        from .stream import AudioStream

        if not isinstance(stream, AudioStream) or stream.media_id != self.id:
            raise GamdlMediaError("Stream does not belong to this media resource")
        if not isinstance(backend, DrmBackend):
            backend = DrmBackend(backend)
        return await get_stream_keys(
            self.api,
            stream=stream,
            media_id=self.id,
            is_library=self.is_library,
            backend=backend,
            device_path=device_path,
            cdm=cdm,
        )
