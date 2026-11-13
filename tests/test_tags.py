"""Offline contracts for download-side tag resolution and legacy metadata fallbacks."""

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from gamdl.api.exceptions import GamdlApiResponseError
from gamdl.download import TagResolutionError, TagResolver, Tags
from gamdl.download.tags import DEFAULT_DATE_TAG_TEMPLATE, format_date_tag
from gamdl.media import MusicVideo, Song, SongCodec, UploadedVideo
from gamdl.media.song.stream import SongStreamResolver
from tests.test_media import PLAYBACK_TAG_DATA, SONG_DATA, VIDEO_DATA, fake_api


def wrapper_payload(data=None):
    return {
        "songList": [
            {
                "hls-playlist-url": "https://cdn.example.test/master.m3u8",
                "assets": [{"metadata": data or PLAYBACK_TAG_DATA}],
            }
        ]
    }


def fake_wrapper(*, payload=None):
    wrapper = Mock()
    wrapper.get_playback = AsyncMock(return_value=payload or wrapper_payload())
    return wrapper


def fake_itunes(*, track=None, collection=None, page=None):
    itunes = Mock()
    itunes.storefront_id = 143441
    records = ([track] if track is not None else []) + (
        [collection] if collection is not None else []
    )
    itunes.get_lookup_result = AsyncMock(return_value={"results": records})
    itunes.get_itunes_page = AsyncMock(
        return_value={
            "storePlatformData": {
                "product-dv": {
                    "results": {
                        "299199411": page
                        or {
                            "collectionId": 1559885420,
                            "copyright": "Copyright test",
                            "genres": [{"genreId": 14}],
                        }
                    }
                }
            }
        }
    )
    return itunes


VIDEO_TRACK = {
    "wrapperType": "track",
    "trackName": "Never Gonna Give You Up",
    "artistName": "Rick Astley",
    "artistId": 669771,
    "releaseDate": "1987-07-27T00:00:00Z",
    "primaryGenreName": "Pop",
    "trackExplicitness": "notExplicit",
    "trackNumber": 1,
    "trackCount": 10,
    "discNumber": 1,
    "discCount": 1,
}
VIDEO_COLLECTION = {
    "wrapperType": "collection",
    "collectionName": "Whenever You Need Somebody",
    "collectionId": 1559885420,
    "artistName": "Rick Astley",
    "releaseDate": "1987-07-27T00:00:00Z",
}


class TagModelTests(unittest.TestCase):
    def test_legacy_date_tag_template_and_partial_dates(self):
        self.assertEqual(DEFAULT_DATE_TAG_TEMPLATE, "%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(
            format_date_tag("1990-04-12", DEFAULT_DATE_TAG_TEMPLATE),
            "1990-04-12T00:00:00Z",
        )
        self.assertEqual(
            format_date_tag("2020-03-04T12:34:56Z", DEFAULT_DATE_TAG_TEMPLATE),
            "2020-03-04T12:34:56Z",
        )
        self.assertEqual(format_date_tag("1990-04-12", "%Y"), "1990")
        self.assertEqual(format_date_tag("1990-04", "%Y/%m/%d"), "1990/04/01")
        self.assertEqual(format_date_tag("1990", "%Y-%m"), "1990-01")
        self.assertEqual(format_date_tag("not-a-date", "%Y"), "not-a-date")
        self.assertIsNone(format_date_tag(None, "%Y"))
        for invalid in ("", " ", None, False, 123):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ValueError, "date_tag_template"),
            ):
                format_date_tag("2020-01-01", invalid)

    def test_playback_asset_normalization_and_media_types(self):
        tags = Tags.from_asset_metadata(PLAYBACK_TAG_DATA)
        self.assertEqual(tags.title, "Never Gonna Give You Up")
        self.assertEqual(tags.artist_id, 669771)
        self.assertEqual(tags.album_id, 1559885420)
        self.assertEqual(tags.track_total, 10)
        self.assertEqual(tags.genre, ("Pop",))
        self.assertEqual(tags.xid, PLAYBACK_TAG_DATA["xid"])
        self.assertIs(tags.explicit, False)
        self.assertEqual((tags.media_type, tags.rating), (1, 0))
        video = Tags.from_asset_metadata(
            {
                **PLAYBACK_TAG_DATA,
                "kind": "music-video",
                "explicit": 1,
            }
        )
        self.assertEqual((video.media_type, video.rating), (6, 1))

    def test_flag_parsing_and_missing_title(self):
        tags = Tags.from_asset_metadata(
            {
                **PLAYBACK_TAG_DATA,
                "compilation": "0",
                "gapless": "1",
            }
        )
        self.assertIs(tags.compilation, False)
        self.assertIs(tags.gapless, True)
        with self.assertRaises(TagResolutionError):
            Tags.from_asset_metadata({"artistName": "Some artist"})
        with self.assertRaisesRegex(TagResolutionError, "numeric tag"):
            Tags.from_asset_metadata(
                {
                    **PLAYBACK_TAG_DATA,
                    "albumId": "not-a-number",
                    "playlistId": "not-a-number",
                }
            )


class TagResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_song_uses_webplayback_without_catalog_and_memoizes(self):
        api = fake_api()
        song = Song.from_id(SONG_DATA["id"], api=api)
        resolver = TagResolver()
        api.get_webplayback.assert_not_awaited()
        tags = await resolver.get_tags(song)
        self.assertIs(tags, await resolver.get_tags(song))
        self.assertEqual(tags.title, PLAYBACK_TAG_DATA["itemName"])
        api.get_webplayback.assert_awaited_once_with(song.id, is_library=False)
        api.get_song.assert_awaited_once_with(song.id)

    async def test_concurrent_webplayback_and_tag_requests_share_fetch(self):
        api = fake_api()
        song = Song.from_id("1559885421", api=api)
        result, tags = await asyncio.gather(
            song._get_webplayback(),
            TagResolver().get_tags(song),
        )
        self.assertEqual(
            result["songList"][0]["assets"][0]["metadata"]["itemName"], tags.title
        )
        api.get_webplayback.assert_awaited_once()

    async def test_wrapper_metadata_preferred_and_reused_after_stream_discovery(self):
        api = fake_api()
        wrapper = fake_wrapper(
            payload=wrapper_payload(
                {
                    **PLAYBACK_TAG_DATA,
                    "itemName": "Wrapper Title",
                }
            )
        )
        song = Song.from_id("1559885421", api=api)
        stream_resolver = SongStreamResolver(song, wrapper_api=wrapper)
        self.assertIsNotNone(await stream_resolver._wrapper_hls_source())
        tags = await TagResolver(wrapper_api=wrapper).get_tags(song)
        self.assertEqual(tags.title, "Wrapper Title")
        wrapper.get_playback.assert_awaited_once_with(song.id)
        api.get_webplayback.assert_not_awaited()

    async def test_wrapper_missing_metadata_does_not_fall_back_to_webplayback(self):
        api = fake_api()
        wrapper = fake_wrapper(payload={"songList": [{"assets": [{}]}]})
        song = Song.from_id("1559885421", api=api)
        with self.assertRaisesRegex(TagResolutionError, "no usable tag metadata"):
            await TagResolver(wrapper_api=wrapper).get_tags(song, codec=SongCodec.AAC)
        api.get_webplayback.assert_not_awaited()

    async def test_wrapper_api_failure_does_not_fall_back(self):
        api = fake_api()
        wrapper = fake_wrapper()
        wrapper.get_playback.side_effect = GamdlApiResponseError("unavailable")
        song = Song.from_id("1559885421", api=api)
        with self.assertRaisesRegex(GamdlApiResponseError, "unavailable"):
            await TagResolver(wrapper_api=wrapper).get_tags(
                song, codec=SongCodec.ALAC_HIRES
            )
        api.get_webplayback.assert_not_awaited()

    async def test_web_codecs_use_webplayback_even_with_wrapper(self):
        api = fake_api()
        wrapper = fake_wrapper(
            payload=wrapper_payload(
                {**PLAYBACK_TAG_DATA, "itemName": "Only in Wrapper"}
            )
        )
        song = Song.from_id("1559885421", api=api)
        resolver = TagResolver(wrapper_api=wrapper)
        web = await resolver.get_tags(song, codec=SongCodec.AAC_WEB)
        he_web = await resolver.get_tags(song, codec=SongCodec.AAC_HE_WEB)
        self.assertIs(web, he_web)
        self.assertEqual(web.title, PLAYBACK_TAG_DATA["itemName"])
        api.get_webplayback.assert_awaited_once()
        wrapper.get_playback.assert_not_awaited()

    async def test_web_audio_and_tags_share_one_webplayback_request(self):
        api = fake_api()
        api.get_webplayback.return_value["songList"][0]["assets"].append(
            {"flavor": "28:ctrp256", "URL": "https://cdn.example.test/aac.m4a"}
        )
        wrapper = fake_wrapper()
        song = Song.from_id("1559885421", api=api)
        stream = await song.get_stream(codec=SongCodec.AAC_WEB, wrapper_api=wrapper)
        tags = await TagResolver(wrapper_api=wrapper).get_tags(song, codec=stream.codec)
        self.assertEqual(tags.title, PLAYBACK_TAG_DATA["itemName"])
        self.assertEqual(stream.codec, SongCodec.AAC_WEB)
        api.get_webplayback.assert_awaited_once()
        api.get_assets.assert_not_awaited()
        wrapper.get_playback.assert_not_awaited()

    async def test_same_song_web_and_wrapper_tags_are_cached_separately(self):
        api = fake_api()
        wrapper = fake_wrapper(
            payload=wrapper_payload(
                {**PLAYBACK_TAG_DATA, "itemName": "Only in Wrapper"}
            )
        )
        song = Song.from_id("1559885421", api=api)
        resolver = TagResolver(wrapper_api=wrapper)
        web = await resolver.get_tags(song, codec=SongCodec.AAC_WEB)
        wrapper_tags = await resolver.get_tags(song, codec=SongCodec.AAC)
        self.assertEqual(web.title, PLAYBACK_TAG_DATA["itemName"])
        self.assertEqual(wrapper_tags.title, "Only in Wrapper")
        self.assertIs(
            wrapper_tags, await resolver.get_tags(song, codec=SongCodec.ALAC_HIRES)
        )
        wrapper.get_playback.assert_awaited_once_with(song.id)
        api.get_webplayback.assert_awaited_once()

    async def test_nonweb_codec_without_wrapper_uses_webplayback_tags(self):
        api = fake_api()
        song = Song.from_id("1559885421", api=api)
        tags = await TagResolver().get_tags(song, codec=SongCodec.AAC)
        self.assertEqual(tags.title, PLAYBACK_TAG_DATA["itemName"])
        api.get_webplayback.assert_awaited_once()

    async def test_library_song_uses_webplayback_not_wrapper(self):
        api = fake_api()
        wrapper = fake_wrapper()
        song = Song.from_id("i.private", api=api, is_library=True)
        tags = await TagResolver(wrapper_api=wrapper).get_tags(song)
        self.assertEqual(tags.title, PLAYBACK_TAG_DATA["itemName"])
        wrapper.get_playback.assert_not_awaited()
        api.get_webplayback.assert_awaited_once_with("i.private", is_library=True)

    async def test_missing_webplayback_data_retries_after_invalidate(self):
        api = fake_api()
        song = Song.from_id("1559885421", api=api)
        resolver = TagResolver()
        api.get_webplayback.return_value = {"songList": []}
        with self.assertRaises(TagResolutionError):
            await resolver.get_tags(song)
        song.invalidate()
        api.get_webplayback.return_value = {
            "songList": [{"assets": [{"metadata": PLAYBACK_TAG_DATA}]}]
        }
        self.assertEqual(
            (await resolver.get_tags(song)).title, "Never Gonna Give You Up"
        )
        self.assertEqual(api.get_webplayback.await_count, 2)

    async def test_song_does_not_fetch_optional_itunes_when_already_complete(self):
        itunes = fake_itunes(collection=VIDEO_COLLECTION)
        tags = await TagResolver(itunes_api=itunes).get_tags(
            Song.from_id("1559885421", api=fake_api())
        )
        self.assertEqual(tags.album, "Whenever You Need Somebody")
        itunes.get_lookup_result.assert_not_awaited()

    async def test_song_never_enriches_missing_tags_from_itunes_by_default(self):
        api = fake_api()
        api.get_webplayback.return_value = {
            "songList": [
                {
                    "assets": [
                        {
                            "metadata": {
                                **PLAYBACK_TAG_DATA,
                                "playlistName": None,
                                "releaseDate": "1990-01-01",
                                "trackCount": None,
                            }
                        }
                    ]
                }
            ]
        }
        itunes = fake_itunes(track=VIDEO_TRACK, collection=VIDEO_COLLECTION)
        tags = await TagResolver(itunes_api=itunes).get_tags(
            Song.from_id("1559885421", api=api)
        )
        self.assertIsNone(tags.album)
        self.assertIsNone(tags.track_total)
        self.assertEqual(tags.release_date, "1990-01-01")
        itunes.get_lookup_result.assert_not_awaited()

    async def test_use_album_date_overrides_only_date_not_other_tags(self):
        api = fake_api()
        api.get_webplayback.return_value = {
            "songList": [
                {
                    "assets": [
                        {
                            "metadata": {
                                **PLAYBACK_TAG_DATA,
                                "playlistName": None,
                                "trackCount": None,
                                "releaseDate": "1990-01-01",
                            }
                        }
                    ]
                }
            ]
        }
        itunes = fake_itunes(track=VIDEO_TRACK, collection=VIDEO_COLLECTION)
        tags = await TagResolver(itunes_api=itunes, use_album_date=True).get_tags(
            Song.from_id("1559885421", api=api)
        )
        self.assertIsNone(tags.album)
        self.assertIsNone(tags.track_total)
        self.assertEqual(tags.release_date, VIDEO_COLLECTION["releaseDate"])
        itunes.get_lookup_result.assert_awaited_once_with("1559885420")

    async def test_use_album_date_requires_itunes_api(self):
        with self.assertRaisesRegex(ValueError, "requires an iTunes API"):
            TagResolver(use_album_date=True)

    async def test_use_album_date_without_album_id_uses_playback_date(self):
        api = fake_api()
        api.get_webplayback.return_value = {
            "songList": [
                {
                    "assets": [
                        {
                            "metadata": {
                                **PLAYBACK_TAG_DATA,
                                "playlistId": None,
                                "releaseDate": "1990-01-01",
                            }
                        }
                    ]
                }
            ]
        }
        itunes = fake_itunes(collection=VIDEO_COLLECTION)
        tags = await TagResolver(itunes_api=itunes, use_album_date=True).get_tags(
            Song.from_id("1559885421", api=api)
        )
        self.assertEqual(tags.release_date, "1990-01-01")
        itunes.get_lookup_result.assert_not_awaited()

    async def test_use_album_date_without_itunes_result_preserves_playback_date(self):
        itunes = fake_itunes()
        tags = await TagResolver(itunes_api=itunes, use_album_date=True).get_tags(
            Song.from_id("1559885421", api=fake_api())
        )
        self.assertEqual(tags.release_date, PLAYBACK_TAG_DATA["releaseDate"])
        itunes.get_lookup_result.assert_awaited_once_with("1559885420")

    async def test_use_album_date_caches_lookup_across_song_instances(self):
        api = fake_api()
        itunes = fake_itunes(collection=VIDEO_COLLECTION)
        resolver = TagResolver(itunes_api=itunes, use_album_date=True)
        first, second = await asyncio.gather(
            resolver.get_tags(Song.from_id("1559885421", api=api)),
            resolver.get_tags(Song.from_id("1559885422", api=api)),
        )
        self.assertEqual(first.release_date, VIDEO_COLLECTION["releaseDate"])
        self.assertEqual(second.release_date, VIDEO_COLLECTION["releaseDate"])
        itunes.get_lookup_result.assert_awaited_once_with("1559885420")

    async def test_album_date_lookup_failure_keeps_song_tags_and_is_retryable(self):
        itunes = fake_itunes(collection=VIDEO_COLLECTION)
        itunes.get_lookup_result.side_effect = [
            GamdlApiResponseError("iTunes unavailable"),
            {"results": [VIDEO_COLLECTION]},
        ]
        resolver = TagResolver(itunes_api=itunes, use_album_date=True)
        first = await resolver.get_tags(Song.from_id("1559885421", api=fake_api()))
        second = await resolver.get_tags(Song.from_id("1559885422", api=fake_api()))
        self.assertEqual(first.release_date, PLAYBACK_TAG_DATA["releaseDate"])
        self.assertEqual(second.release_date, VIDEO_COLLECTION["releaseDate"])
        self.assertEqual(itunes.get_lookup_result.await_count, 2)

    async def test_resolvers_with_different_sources_do_not_share_tag_result(self):
        api = fake_api()
        song = Song.from_id("1559885421", api=api)
        wrapper = fake_wrapper(
            payload=wrapper_payload(
                {
                    **PLAYBACK_TAG_DATA,
                    "itemName": "Different Wrapper Title",
                }
            )
        )
        wrapper_tags = await TagResolver(wrapper_api=wrapper).get_tags(song)
        web_tags = await TagResolver().get_tags(song)
        self.assertEqual(wrapper_tags.title, "Different Wrapper Title")
        self.assertEqual(web_tags.title, PLAYBACK_TAG_DATA["itemName"])

    async def test_video_wrapper_tags_without_itunes(self):
        api = fake_api()
        video = MusicVideo.from_id("299199411", api=api)
        wrapper = fake_wrapper(
            payload=wrapper_payload(
                {
                    **PLAYBACK_TAG_DATA,
                    "kind": "music-video",
                    "itemName": "Wrapper Video",
                }
            )
        )
        tags = await TagResolver(wrapper_api=wrapper).get_tags(video)
        self.assertEqual(tags.title, "Wrapper Video")
        self.assertEqual(tags.media_type, 6)
        api.get_music_video.assert_awaited_once_with(video.id)
        wrapper.get_playback.assert_awaited_once_with(video.id)

    async def test_video_itunes_page_lookup_and_album_enrichment(self):
        api = fake_api()
        api.get_album = AsyncMock(
            return_value={"data": [{"attributes": {"isCompilation": True}}]}
        )
        itunes = fake_itunes(track=VIDEO_TRACK, collection=VIDEO_COLLECTION)
        video = MusicVideo.from_id("299199411", api=api)
        tags = await TagResolver(itunes_api=itunes).get_tags(video)
        self.assertEqual(tags.title, "Never Gonna Give You Up")
        self.assertEqual(tags.album, "Whenever You Need Somebody")
        self.assertEqual(tags.album_id, 1559885420)
        self.assertEqual(tags.track_number, 1)
        self.assertEqual(tags.track_total, 10)
        self.assertEqual(tags.genre_id, 14)
        self.assertEqual(tags.rating, 0)
        self.assertEqual(tags.media_type, 6)
        self.assertIs(tags.compilation, True)
        itunes.get_itunes_page.assert_awaited_once_with("music-video", "299199411")
        itunes.get_lookup_result.assert_awaited_once_with("299199411")
        api.get_album.assert_awaited_once_with("1559885420")

    async def test_video_fallback_if_wrapper_rejects(self):
        api = fake_api()
        api.get_album = AsyncMock(return_value={"data": []})
        wrapper = fake_wrapper(payload={"songList": []})
        itunes = fake_itunes(track=VIDEO_TRACK, collection=VIDEO_COLLECTION)
        video = MusicVideo.from_id("299199411", api=api)
        tags = await TagResolver(wrapper_api=wrapper, itunes_api=itunes).get_tags(video)
        self.assertEqual(tags.title, VIDEO_TRACK["trackName"])
        wrapper.get_playback.assert_awaited_once()

    async def test_video_album_enrichment_failure_keeps_lookup_tags(self):
        api = fake_api()
        api.get_album = AsyncMock(
            side_effect=GamdlApiResponseError("Album unavailable")
        )
        itunes = fake_itunes(track=VIDEO_TRACK, collection=VIDEO_COLLECTION)
        tags = await TagResolver(itunes_api=itunes).get_tags(
            MusicVideo.from_id("299199411", api=api)
        )
        self.assertEqual(tags.album, VIDEO_COLLECTION["collectionName"])
        self.assertIsNone(tags.compilation)

    async def test_video_lookup_with_missing_title_rejected(self):
        api = fake_api()
        itunes = fake_itunes(track={**VIDEO_TRACK, "trackName": None})
        with self.assertRaisesRegex(TagResolutionError, "no title"):
            await TagResolver(itunes_api=itunes).get_tags(
                MusicVideo.from_id("299199411", api=api)
            )

    async def test_video_standalone_has_no_album_lookup(self):
        api = fake_api()
        api.get_album = AsyncMock()
        itunes = fake_itunes(track=VIDEO_TRACK)
        video = MusicVideo.from_id("299199411", api=api)
        tags = await TagResolver(itunes_api=itunes).get_tags(video)
        self.assertIsNone(tags.album)
        self.assertIsNone(tags.album_id)
        api.get_album.assert_not_awaited()

    async def test_video_uses_id_from_catalog_url(self):
        api = fake_api()
        api.get_music_video.return_value = {
            "data": [
                {
                    **VIDEO_DATA,
                    "attributes": {
                        "name": "Video",
                        "url": "https://music.apple.com/us/music-video/name/299199411?i=1",
                        "playParams": {"id": "different-id", "kind": "musicVideo"},
                    },
                }
            ]
        }
        itunes = fake_itunes(track=VIDEO_TRACK)
        video = MusicVideo.from_id("different-id", api=api)
        await TagResolver(itunes_api=itunes).get_tags(video)
        itunes.get_lookup_result.assert_awaited_once_with("299199411")

    async def test_video_without_wrapper_or_itunes_has_explicit_error(self):
        video = MusicVideo.from_id("299199411", api=fake_api())
        with self.assertRaisesRegex(TagResolutionError, "iTunes API"):
            await TagResolver().get_tags(video)

    async def test_video_incomplete_lookup_has_explicit_error(self):
        api = fake_api()
        video = MusicVideo.from_id("299199411", api=api)
        itunes = fake_itunes()
        with self.assertRaisesRegex(TagResolutionError, "no track metadata"):
            await TagResolver(itunes_api=itunes).get_tags(video)

    async def test_uploaded_video_not_in_scope(self):
        uploaded = UploadedVideo.from_id("2", api=fake_api())
        with self.assertRaises(TagResolutionError):
            await TagResolver().get_tags(uploaded)
