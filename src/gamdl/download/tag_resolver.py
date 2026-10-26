"""Lazy tag resolution, independent of MP4 atom encoding and file I/O."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from async_lru import alru_cache

from gamdl.api.exceptions import GamdlApiError
from gamdl.media import Media, MusicVideo, Song, UploadedVideo

from .exceptions import TagResolutionError
from .tags import Tags

if TYPE_CHECKING:
    from gamdl.api import ItunesApi
    from gamdl.api.wrapper import WrapperApi


def _playback_asset(playback: dict[str, Any]) -> dict[str, Any]:
    """Find a usable playback metadata record without trusting asset order."""
    entries = playback.get("songList")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            assets = entry.get("assets")
            if not isinstance(assets, list):
                continue
            for asset in assets:
                metadata = asset.get("metadata") if isinstance(asset, dict) else None
                if (
                    isinstance(metadata, dict)
                    and isinstance(metadata.get("itemName"), str)
                    and metadata["itemName"]
                ):
                    return metadata
    raise TagResolutionError("Playback has no usable tag metadata")


def _number(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _lookup_parts(
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    entries = payload.get("results")
    if not isinstance(entries, list):
        return None, None
    track = next(
        (
            item
            for item in entries
            if isinstance(item, dict) and item.get("wrapperType") == "track"
        ),
        None,
    )
    collection = next(
        (
            item
            for item in entries
            if isinstance(item, dict) and item.get("wrapperType") == "collection"
        ),
        None,
    )
    return track, collection


class TagResolver:
    """Construct output-oriented Tags using lazy shared playback responses.

    Wrapper playback is preferred when configured for catalog songs/videos.
    A failed or incomplete wrapper response falls back to webplayback (songs)
    or iTunes page + lookup metadata (music videos). For songs, iTunes is
    consulted only when use_album_date=True, and only for an album's release
    date. Neither file tags nor media bytes are written here.
    """

    def __init__(
        self,
        *,
        wrapper_api: WrapperApi | None = None,
        itunes_api: ItunesApi | None = None,
        use_album_date: bool = False,
    ) -> None:
        if use_album_date and itunes_api is None:
            raise ValueError("use_album_date requires an iTunes API")
        self.wrapper_api = wrapper_api
        self.itunes_api = itunes_api
        self.use_album_date = use_album_date
        self._cache_key = object()
        # Album-wide reuse: a playlist/album may contain many songs sharing
        # an album ID. Bound to this resolver, not a global cache of `self`.
        self._album_release_date = alru_cache(maxsize=128, ttl=3600)(
            self._fetch_album_release_date
        )

    async def get_tags(self, media: Media) -> Tags:
        """Resolve tags once per media instance and resolver configuration."""
        if not isinstance(media, (Song, MusicVideo, UploadedVideo)):
            raise TagResolutionError(
                "Tag resolution is not supported for this media type"
            )

        async def load() -> Tags:
            if isinstance(media, Song):
                return await self._song_tags(media)
            if isinstance(media, UploadedVideo):
                return await self._uploaded_video_tags(media)
            return await self._video_tags(media)

        return await media._load(("download_tags", self._cache_key), load)

    async def _wrapper_tags(self, media: Media) -> Tags | None:
        if self.wrapper_api is None or media.is_library:
            return None
        try:
            payload = await media._get_wrapper_playback(self.wrapper_api)
            return Tags.from_asset_metadata(_playback_asset(payload))
        except (GamdlApiError, TagResolutionError, KeyError, TypeError, ValueError):
            return None

    async def _song_tags(self, song: Song) -> Tags:
        tags = await self._wrapper_tags(song)
        if tags is None:
            tags = Tags.from_asset_metadata(
                _playback_asset(await song._get_webplayback())
            )
        # Song tags come only from playback metadata. The optional album-date
        # override is the *sole* reason to query iTunes for a song.
        if not self.use_album_date or tags.album_id is None:
            return tags
        try:
            date = await self._album_release_date(tags.album_id)
        except GamdlApiError:
            # A temporary iTunes failure should not discard otherwise complete
            # playback tags, and must not become a cached lookup result.
            return tags
        return replace(tags, release_date=date) if date else tags

    async def _fetch_album_release_date(self, album_id: int) -> str | None:
        """Fetch only the album release date; never enrich other song fields."""
        assert self.itunes_api is not None
        lookup = await self.itunes_api.get_lookup_result(str(album_id))
        _, collection = _lookup_parts(lookup)
        return _text(collection.get("releaseDate")) if collection else None

    async def _video_tags(self, video: MusicVideo) -> Tags:
        tags = await self._wrapper_tags(video)
        if tags is not None:
            return replace(tags, media_type=6)

        if self.itunes_api is None:
            raise TagResolutionError(
                "Music video tags require wrapper playback or an iTunes API"
            )
        metadata = await video.get_metadata()
        attributes = metadata.get("attributes", {})
        media_url = attributes.get("url") if isinstance(attributes, dict) else None
        catalog_id = await video._catalog_id() if video.is_library else video.id
        url_id = (
            urlsplit(media_url).path.rsplit("/", 1)[-1]
            if isinstance(media_url, str) and media_url
            else catalog_id
        )
        if not url_id or not url_id.isdigit():
            url_id = catalog_id
        page = await self.itunes_api.get_itunes_page("music-video", url_id)
        page_data = (
            page.get("storePlatformData", {})
            .get("product-dv", {})
            .get("results", {})
            .get(url_id)
        )
        if not isinstance(page_data, dict):
            raise TagResolutionError("iTunes music-video page metadata is unavailable")
        track, collection = _lookup_parts(
            await self.itunes_api.get_lookup_result(url_id)
        )
        if track is None:
            raise TagResolutionError("iTunes music-video lookup has no track metadata")

        album_id = _number(page_data.get("collectionId") or track.get("collectionId"))
        album_info = None
        if collection is not None and album_id is not None:
            # Additional album fields are optional. The lookup already provides
            # most tags; only request the catalog album when compilation is needed.
            try:
                result = await video.api.get_album(str(album_id))
            except GamdlApiError:
                pass
            else:
                values = result.get("data")
                if isinstance(values, list) and values and isinstance(values[0], dict):
                    album_info = values[0].get("attributes")

        genre_rows = page_data.get("genres")
        genre_id = (
            _number(genre_rows[0].get("genreId"))
            if isinstance(genre_rows, list)
            and genre_rows
            and isinstance(genre_rows[0], dict)
            else None
        )
        explicitness = track.get("trackExplicitness")
        rating = {"notExplicit": 0, "explicit": 1, "cleaned": 2}.get(explicitness)
        title = _text(track.get("trackName"))
        if title is None:
            raise TagResolutionError("iTunes music-video lookup has no title")
        return Tags(
            title=title,
            artist=_text(track.get("artistName")),
            artist_id=_number(track.get("artistId")),
            album=_text(collection.get("collectionName")) if collection else None,
            album_artist=_text(collection.get("artistName")) if collection else None,
            album_id=album_id if collection is not None else None,
            release_date=_text(track.get("releaseDate")),
            genre=(
                (track["primaryGenreName"],)
                if _text(track.get("primaryGenreName"))
                else ()
            ),
            genre_id=genre_id,
            copyright=_text(page_data.get("copyright")),
            title_id=_number(catalog_id),
            storefront=self.itunes_api.storefront_id,
            track_number=_number(track.get("trackNumber")) if collection else None,
            track_total=_number(track.get("trackCount")) if collection else None,
            disc_number=_number(track.get("discNumber")) if collection else None,
            disc_total=_number(track.get("discCount")) if collection else None,
            compilation=(
                album_info.get("isCompilation")
                if isinstance(album_info, dict)
                else None
            ),
            explicit=(rating == 1) if rating is not None else None,
            rating=rating,
            media_type=6,
        )

    async def _uploaded_video_tags(self, video: UploadedVideo) -> Tags:
        """Uploaded videos have catalog metadata but no playback tag asset."""
        record = await video.get_metadata()
        attributes = record.get("attributes", {})
        if not isinstance(attributes, dict):
            attributes = {}
        name = _text(attributes.get("name"))
        if name is None:
            raise TagResolutionError("Uploaded video has no title")
        return Tags(
            title=name,
            artist=_text(attributes.get("artistName")),
            release_date=_text(attributes.get("uploadDate")),
            title_id=_number(record.get("id")),
            storefront=self.itunes_api.storefront_id if self.itunes_api else None,
            media_type=6,
        )
