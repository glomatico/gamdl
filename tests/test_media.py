"""Offline contracts for the lazy media layer and source resolution."""

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from gamdl.media import (
    DEFAULT_AUTO_SELECT,
    Cover,
    GamdlMediaError,
    Lyrics,
    Media,
    MediaResolver,
    MusicVideo,
    SelectionKind,
    SelectionRequiredError,
    Song,
    Tags,
    UnsupportedMediaError,
    UploadedVideo,
)

SONG_DATA = {
    "id": "1559885421",
    "type": "songs",
    "attributes": {
        "name": "Never Gonna Give You Up",
        "artistName": "Rick Astley",
        "albumName": "Whenever You Need Somebody",
        "trackNumber": 1,
        "artwork": {"url": "https://example.test/{w}x{h}.{f}", "width": 2000},
    },
}
VIDEO_DATA = {
    "id": "299199411",
    "type": "music-videos",
    "attributes": {"name": "Never Gonna Give You Up"},
}
PLAYBACK_TAG_DATA = {
    "kind": "song",
    "itemId": "1559885421",
    "itemName": "Never Gonna Give You Up",
    "artistName": "Rick Astley",
    "artistId": "669771",
    "playlistName": "Whenever You Need Somebody",
    "playlistId": "1559885420",
    "playlistArtistName": "Rick Astley",
    "sort-name": "Never Gonna Give You Up",
    "sort-artist": "Rick Astley",
    "trackNumber": 1,
    "trackCount": 10,
    "discNumber": 1,
    "discCount": 1,
    "genre": "Pop",
    "genreId": 14,
    "composerName": "Mike Stock, Matt Aitken & Pete Waterman",
    "releaseDate": "1987-07-27T07:00:00Z",
    "explicit": 0,
    "s": 143503,
    "xid": "BMGRightsManagementUKLtd:isrc:GBARL9300135",
}
TTML = (
    '<tt xmlns="http://www.w3.org/ns/ttml" xml:lang="en">'
    '<body><div><p begin="00:00:01.200" end="00:00:02.500">'
    '<span begin="1.2s" end="1.7s">Hello </span>'
    '<span begin="1.7s" end="2.5s">world</span></p>'
    '<p begin="3.000s" end="4.000s">Another line</p></div>'
    "<div><p>Untimed line</p></div></body></tt>"
)


def fake_api():
    api = Mock()
    api.get_song = AsyncMock(return_value={"data": [SONG_DATA]})
    api.get_library_song = AsyncMock(return_value={"data": [SONG_DATA]})
    api.get_lyrics = AsyncMock(return_value={"data": [{"attributes": {"ttml": TTML}}]})
    api.get_syllable_lyrics = AsyncMock(
        return_value={"data": [{"attributes": {"ttml": TTML}}]}
    )
    api.get_assets = AsyncMock(return_value={"results": {"assets": [{"url": "test"}]}})
    api.get_webplayback = AsyncMock(
        return_value={
            "songList": [
                {"songId": "1559885421", "assets": [{"metadata": PLAYBACK_TAG_DATA}]}
            ]
        }
    )
    api.get_music_video = AsyncMock(return_value={"data": [VIDEO_DATA]})
    api.get_uploaded_video = AsyncMock(
        return_value={"data": [{"id": "2", "type": "uploaded-videos"}]}
    )
    return api


class MediaFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_lazy_from_id_and_prefetched_from_metadata(self):
        api = fake_api()
        song = Song.from_id(SONG_DATA["id"], api=api)
        self.assertEqual(song.type, "songs")
        api.get_song.assert_not_awaited()
        self.assertEqual(await song.get_metadata(), SONG_DATA)
        self.assertEqual(await song.get_metadata(), SONG_DATA)
        api.get_song.assert_awaited_once_with(SONG_DATA["id"])

        seeded = Media.from_metadata(SONG_DATA, api=api)
        self.assertIsInstance(seeded, Song)
        self.assertEqual(await seeded.get_metadata(), SONG_DATA)
        api.get_song.assert_awaited_once()
        self.assertIsInstance(Media.from_metadata(VIDEO_DATA, api=api), MusicVideo)
        self.assertIsInstance(
            Media.from_id("2", api=api, media_type="uploaded-videos"), UploadedVideo
        )
        self.assertFalse(issubclass(UploadedVideo, MusicVideo))

    async def test_library_factories_and_wrong_types(self):
        api = fake_api()
        media = Media.from_metadata(
            {"type": "library-songs", "id": "i.123", "attributes": {}}, api=api
        )
        self.assertIsInstance(media, Song)
        self.assertTrue(media.is_library)
        self.assertEqual(media.type, "library-songs")
        with self.assertRaises(UnsupportedMediaError):
            Media.from_id("123", api=api)
        with self.assertRaises(UnsupportedMediaError):
            Song.from_metadata(VIDEO_DATA, api=api)
        with self.assertRaises(UnsupportedMediaError):
            Media.from_metadata({"id": "11", "type": "albums"}, api=api)
        with self.assertRaises(UnsupportedMediaError):
            UploadedVideo.from_id("22", api=api, is_library=True)

    async def test_video_specific_metadata_endpoints(self):
        api = fake_api()
        video = MusicVideo.from_id("299199411", api=api)
        uploaded = UploadedVideo.from_id("2", api=api)
        self.assertEqual((await video.get_metadata())["type"], "music-videos")
        self.assertEqual((await uploaded.get_metadata())["type"], "uploaded-videos")
        api.get_music_video.assert_awaited_once_with("299199411")
        api.get_uploaded_video.assert_awaited_once_with("2")

    async def test_concurrent_fetch_is_shared_and_cancel_safe(self):
        started = asyncio.Event()
        allow = asyncio.Event()

        async def fetch(_media_id):
            started.set()
            await allow.wait()
            return {"data": [SONG_DATA]}

        api = fake_api()
        api.get_song.side_effect = fetch
        song = Song.from_id(SONG_DATA["id"], api=api)
        first = asyncio.create_task(song.get_metadata())
        await started.wait()
        second = asyncio.create_task(song.get_metadata())
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        allow.set()
        self.assertEqual(await second, SONG_DATA)
        self.assertEqual(await song.get_metadata(), SONG_DATA)
        api.get_song.assert_awaited_once()

    async def test_failure_retry_and_invalidation(self):
        api = fake_api()
        api.get_song.side_effect = [RuntimeError("temporary"), {"data": [SONG_DATA]}]
        song = Song.from_id(SONG_DATA["id"], api=api)
        with self.assertRaises(RuntimeError):
            await song.get_metadata()
        self.assertEqual(await song.get_metadata(), SONG_DATA)
        self.assertEqual(api.get_song.await_count, 2)
        song.invalidate()
        api.get_song.side_effect = None
        self.assertEqual(await song.get_metadata(), SONG_DATA)
        self.assertEqual(api.get_song.await_count, 3)


class SongOperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cover_and_tags_only_require_one_metadata_request(self):
        api = fake_api()
        song = Song.from_id(SONG_DATA["id"], api=api)
        cover, tags = await asyncio.gather(song.get_cover(), song.get_tags())
        self.assertIsInstance(cover, Cover)
        self.assertIsInstance(tags, Tags)
        self.assertEqual(tags.title, "Never Gonna Give You Up")
        self.assertEqual(tags.album, "Whenever You Need Somebody")
        self.assertEqual(tags.album_id, 1559885420)
        self.assertEqual(tags.artist_id, 669771)
        self.assertEqual(tags.track_total, 10)
        self.assertEqual(tags.genre, ("Pop",))
        self.assertEqual(tags.explicit, False)
        self.assertEqual(tags.xid, PLAYBACK_TAG_DATA["xid"])
        self.assertEqual(cover.get_raw_url(), "https://example.test/{w}x{h}.{f}")
        self.assertEqual(
            cover.get_formatted_url(width=1000, height=1000, format="png"),
            "https://example.test/1000x1000.png",
        )
        api.get_song.assert_awaited_once()
        api.get_webplayback.assert_awaited_once_with(SONG_DATA["id"], is_library=False)

    async def test_tags_are_from_webplayback_not_catalog(self):
        api = fake_api()
        song = Song.from_metadata(
            {
                "id": SONG_DATA["id"],
                "type": "songs",
                "attributes": {"name": "A conflicting catalog title"},
            },
            api=api,
        )
        tags = await song.get_tags()
        self.assertEqual(tags.title, PLAYBACK_TAG_DATA["itemName"])
        self.assertEqual(tags.album, PLAYBACK_TAG_DATA["playlistName"])
        self.assertIs(tags, await song.get_tags())
        api.get_song.assert_not_awaited()
        api.get_webplayback.assert_awaited_once()
        self.assertIs(await song._get_webplayback(), await song._get_webplayback())
        api.get_webplayback.assert_awaited_once()

    async def test_tags_share_a_simultaneous_webplayback_load(self):
        api = fake_api()
        song = Song.from_id(SONG_DATA["id"], api=api)
        playback, tags = await asyncio.gather(song._get_webplayback(), song.get_tags())
        self.assertEqual(
            playback["songList"][0]["assets"][0]["metadata"]["itemName"],
            tags.title,
        )
        api.get_webplayback.assert_awaited_once()
        api.get_song.assert_not_awaited()

    async def test_tags_reject_missing_playback_metadata_and_retry(self):
        api = fake_api()
        song = Song.from_id(SONG_DATA["id"], api=api)
        api.get_webplayback.return_value = {"songList": []}
        with self.assertRaises(GamdlMediaError):
            await song.get_tags()
        song.invalidate()
        api.get_webplayback.return_value = {"songList": [{"assets": [{}]}]}
        with self.assertRaises(GamdlMediaError):
            await song.get_tags()
        song.invalidate()
        api.get_webplayback.return_value = {
            "songList": [{"assets": [{"metadata": PLAYBACK_TAG_DATA}]}]
        }
        self.assertEqual((await song.get_tags()).title, "Never Gonna Give You Up")
        self.assertEqual(api.get_webplayback.await_count, 3)

    async def test_tags_reject_incomplete_first_asset(self):
        api = fake_api()
        api.get_webplayback.return_value = {
            "songList": [{"assets": [{"metadata": {"artistName": "Rick Astley"}}]}]
        }
        song = Song.from_id(SONG_DATA["id"], api=api)
        with self.assertRaises(GamdlMediaError):
            await song.get_tags()

    def test_playback_boolean_string_flags(self):
        values = {**PLAYBACK_TAG_DATA, "compilation": "0", "gapless": "1"}
        tags = Tags.from_asset_metadata(values)
        self.assertIs(tags.compilation, False)
        self.assertIs(tags.gapless, True)

    async def test_cover_format_validation_and_missing_artwork(self):
        cover = Cover.from_metadata(SONG_DATA["attributes"]["artwork"])
        with self.assertRaises(ValueError):
            cover.get_formatted_url(width=0, height=200)
        with self.assertRaises(ValueError):
            cover.get_formatted_url(width=100, height=100, format="gif")
        no_cover = Song.from_metadata(
            {"id": "1", "type": "songs", "attributes": {}}, api=fake_api()
        )
        self.assertIsNone(await no_cover.get_cover())

    async def test_lyrics_cached_separately_by_kind_and_locale(self):
        api = fake_api()
        song = Song.from_id("1559885421", api=api)
        lyrics = await song.get_lyrics()
        self.assertIsInstance(lyrics, Lyrics)
        self.assertFalse(lyrics.syllable)
        self.assertIs(lyrics, await song.get_lyrics())
        self.assertEqual(api.get_lyrics.await_count, 1)
        timed = await song.get_lyrics(syllable=True, lyrics_language="pt-BR")
        self.assertIsInstance(timed, Lyrics)
        self.assertTrue(timed.syllable)
        self.assertTrue(timed.is_syllable_synced)
        self.assertEqual(
            timed.to_lrc().splitlines()[0],
            "[00:01.20]<00:01.20>Hello <00:01.70>world",
        )
        self.assertEqual(timed.to_ttml(), TTML)
        with self.assertRaises(GamdlMediaError):
            timed.to_plain_text()
        with self.assertRaises(GamdlMediaError):
            timed.to_srt()
        self.assertIs(
            timed, await song.get_lyrics(syllable=True, lyrics_language="pt-BR")
        )
        api.get_syllable_lyrics.assert_awaited_once_with(
            "1559885421",
            language=None,
            lyrics_language="pt-BR",
            script=None,
            extend="ttmlLocalizations",
        )

    async def test_library_lyrics_resolve_catalog_and_missing_equivalent(self):
        api = fake_api()
        library_song = Song.from_metadata(
            {
                "id": "i.private",
                "type": "library-songs",
                "relationships": {"catalog": {"data": [{"id": "1559885421"}]}},
            },
            api=api,
        )
        self.assertIsInstance(await library_song.get_lyrics(), Lyrics)
        api.get_lyrics.assert_awaited_once_with("1559885421", language=None)
        self.assertEqual(
            (await library_song._get_assets())["results"]["assets"][0]["url"], "test"
        )
        api.get_assets.assert_awaited_once_with(
            "1559885421",
            kind="song",
            include_license_urls=True,
            hls_encryption="CBC",
            hls_profile="enhancedHls",
        )
        await library_song._get_webplayback()
        api.get_webplayback.assert_awaited_once_with("i.private", is_library=True)

        missing = Song.from_metadata(
            {"id": "i.missing", "type": "library-songs"}, api=api
        )
        api.get_library_song.return_value = {"data": [{"id": "i.missing"}]}
        with self.assertRaises(GamdlMediaError):
            await missing.get_lyrics()
        api.get_library_song.assert_awaited_once_with("i.missing")

    async def test_assets_and_webplayback_are_lazy_and_cached(self):
        api = fake_api()
        song = Song.from_id("1559885421", api=api)
        api.get_assets.assert_not_awaited()
        api.get_webplayback.assert_not_awaited()
        await song._get_assets(hls_profile="")
        await song._get_assets(hls_profile="")
        await song._get_assets(hls_profile="enhancedHls")
        self.assertEqual(api.get_assets.await_count, 2)
        await song._get_webplayback()
        await song._get_webplayback()
        api.get_webplayback.assert_awaited_once_with("1559885421", is_library=False)

    async def test_absent_lyrics_returns_none(self):
        api = fake_api()
        api.get_lyrics.return_value = {"data": []}
        song = Song.from_id("1559885421", api=api)
        self.assertIsNone(await song.get_lyrics())
        self.assertIsNone(await song.get_lyrics())
        api.get_lyrics.assert_awaited_once()


class LyricsTests(unittest.TestCase):
    def test_parsed_stanzas_and_formats(self):
        lyrics = Lyrics.from_ttml(TTML)
        self.assertEqual(len(lyrics.stanzas), 2)
        self.assertEqual(len(lyrics.lines), 3)
        self.assertEqual(lyrics.language, "en")
        self.assertEqual(lyrics.lines[0].text, "Hello world")
        self.assertEqual((lyrics.lines[0].start, lyrics.lines[0].end), (1200, 2500))
        self.assertTrue(lyrics.is_synced)
        self.assertTrue(lyrics.is_syllable_synced)
        self.assertEqual(
            lyrics.to_plain_text(), "Hello world\nAnother line\n\nUntimed line"
        )
        self.assertEqual(
            lyrics.to_lrc(),
            "[00:01.20]Hello world\n[00:03.00]Another line\nUntimed line",
        )
        self.assertNotIn("<00:01.20>", lyrics.to_lrc())
        self.assertEqual(
            lyrics.to_srt().split("\n\n")[0],
            "1\n00:00:01,200 --> 00:00:02,500\nHello world",
        )
        self.assertEqual(lyrics.to_ttml(), TTML)

    def test_a2_enhanced_lrc_and_syllable_only_exports(self):
        lyrics = Lyrics.from_ttml(TTML, syllable=True)
        self.assertTrue(lyrics.is_syllable_synced)
        self.assertEqual(len(lyrics.stanzas), 2)
        self.assertEqual(
            lyrics.to_lrc(),
            "[00:01.20]<00:01.20>Hello <00:01.70>world\n"
            "[00:03.00]Another line\nUntimed line",
        )
        self.assertEqual(lyrics.to_ttml(), TTML)
        with self.assertRaisesRegex(GamdlMediaError, "only export"):
            lyrics.to_plain_text()
        with self.assertRaisesRegex(GamdlMediaError, "only export"):
            lyrics.to_srt()
        self.assertFalse(hasattr(lyrics, "to_enhanced_lrc"))

    def test_nested_timed_spans_never_duplicate_lyrics(self):
        # Apple's TTML can contain an untimed wrapper span around background
        # vocals; its timed children must be emitted exactly once, in order.
        ttml = (
            '<tt xmlns="http://www.w3.org/ns/ttml"><body><div>'
            '<p begin="1.00s" end="3.00s">'
            '<span begin="1.00s" end="1.30s">Say </span>'
            '<span role="background">'
            '<span begin="1.30s" end="1.70s">hello </span>'
            '<span begin="1.70s" end="2.20s">again</span>'
            "</span>"
            '<span begin="2.20s" end="3.00s"> now</span>'
            "</p></div></body></tt>"
        )
        lyrics = Lyrics.from_ttml(ttml, syllable=True)
        line = lyrics.lines[0]
        self.assertEqual(line.text, "Say hello again now")
        self.assertEqual(len(line.segments), 4)
        self.assertEqual([s.start for s in line.segments], [1000, 1300, 1700, 2200])
        self.assertEqual(
            lyrics.to_lrc(),
            "[00:01.00]<00:01.00>Say <00:01.30>hello <00:01.70>again<00:02.20> now",
        )
        self.assertEqual(lyrics.to_ttml(), ttml)

    def test_mixed_timed_and_untimed_text_is_preserved(self):
        ttml = (
            '<tt><body><div><p begin="0.5s" end="2s">'
            'Unmarked <span begin="1s" end="1.5s">word</span> here'
            "</p></div></body></tt>"
        )
        lyrics = Lyrics.from_ttml(ttml, syllable=True)
        self.assertEqual(lyrics.lines[0].text, "Unmarked word here")
        self.assertEqual(lyrics.to_lrc(), "[00:00.50]Unmarked <00:01.00>word here")
        self.assertEqual(lyrics.to_ttml(), ttml)

    def test_line_only_mode_does_not_emit_syllable_timestamps(self):
        lyrics = Lyrics.from_ttml(TTML)
        self.assertTrue(lyrics.is_syllable_synced)  # timing is still parsed
        self.assertFalse(lyrics.syllable)
        self.assertEqual(lyrics.to_lrc().splitlines()[0], "[00:01.20]Hello world")
        self.assertTrue(lyrics.to_plain_text())
        self.assertTrue(lyrics.to_srt())

    def test_invalid_ttml_and_missing_ttml(self):
        with self.assertRaises(GamdlMediaError):
            Lyrics.from_ttml("<xml>")
        with self.assertRaises(GamdlMediaError):
            Lyrics.from_ttml("<not-tt />")
        with self.assertRaises(GamdlMediaError):
            Lyrics.from_ttml('<tt><div><p begin="-1s">Hello</p></div></tt>')
        with self.assertRaises(GamdlMediaError):
            Lyrics(stanzas=[]).to_ttml()


def make_tracks(*ids):
    return [
        {"id": item_id, "type": "songs", "attributes": {"name": f"Track {index}"}}
        for index, item_id in enumerate(ids)
    ]


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = fake_api()
        self.album_tracks = make_tracks("a", "b", "c")
        self.playlist_tracks = make_tracks("a", "a", "b")

        async def iter_album(_id, *, is_library=False):
            for item in self.album_tracks:
                yield item

        async def iter_playlist(_id, *, is_library=False):
            for item in self.playlist_tracks:
                yield item

        self.api.iter_album_tracks = Mock(side_effect=iter_album)
        self.api.iter_playlist_tracks = Mock(side_effect=iter_playlist)

    async def test_auto_selected_album_is_lazy_and_contextual(self):
        resolver = MediaResolver(self.api)
        self.assertEqual(resolver.auto_select, DEFAULT_AUTO_SELECT)
        iterator = resolver.from_album("album")
        self.api.iter_album_tracks.assert_not_called()
        items = [item async for item in iterator]
        self.assertEqual([m.id for m in items], ["a", "b", "c"])
        self.assertEqual([m.source.index for m in items], [0, 1, 2])
        self.assertEqual(items[0].source.id, "album")
        await items[0].get_metadata()
        self.api.get_song.assert_not_awaited()

    async def test_playlist_keeps_duplicate_entries(self):
        items = [x async for x in MediaResolver(self.api).from_playlist("playlist")]
        self.assertEqual([m.id for m in items], ["a", "a", "b"])
        self.assertIsNot(items[0], items[1])
        self.assertEqual(items[1].source.index, 1)

    async def test_required_selection_raises_without_fetching(self):
        resolver = MediaResolver(self.api, auto_select=[])
        with self.assertRaises(SelectionRequiredError) as raised:
            _ = [x async for x in resolver.from_album("album")]
        self.assertEqual(raised.exception.kind, "album_tracks")
        self.api.iter_album_tracks.assert_called_once()
        with self.assertRaises(SelectionRequiredError):
            _ = [x async for x in resolver.from_artist("artist")]
        with self.assertRaises(SelectionRequiredError):
            _ = [x async for x in resolver.from_library()]

    async def test_sync_selector_uses_occurrence_indices(self):
        captured = []

        def choose(request):
            captured.append(request)
            return [1, 1, 2]

        resolver = MediaResolver(self.api, selector=choose, auto_select=[])
        items = [x async for x in resolver.from_playlist("playlist")]
        self.assertEqual([x.id for x in items], ["a", "b"])
        self.assertEqual([x.source.index for x in items], [1, 2])
        self.assertEqual(captured[0].kind, SelectionKind.PLAYLIST_TRACKS)
        self.assertEqual(len(captured[0].options), 3)

    async def test_async_selector_and_invalid_choice(self):
        async def choose(request):
            return [2, 0]

        resolver = MediaResolver(self.api, selector=choose, auto_select=[])
        self.assertEqual([x.id async for x in resolver.from_album("album")], ["a", "c"])
        bad = MediaResolver(self.api, selector=lambda _: [99], auto_select=[])
        with self.assertRaises(ValueError):
            _ = [x async for x in bad.from_playlist("playlist")]
        unhashable = MediaResolver(self.api, selector=lambda _: [[1]], auto_select=[])
        with self.assertRaises(ValueError):
            _ = [x async for x in unhashable.from_playlist("playlist")]

    async def test_url_routing(self):
        resolver = MediaResolver(self.api)
        album_url = "https://music.apple.com/br/album/record/album"
        songs = [x async for x in resolver.from_url(album_url)]
        self.assertEqual(len(songs), 3)
        single = [x async for x in resolver.from_url(album_url + "?i=track123")]
        self.assertEqual(single[0].id, "track123")
        self.assertIsNone(single[0].source)
        video = [
            x
            async for x in resolver.from_url(
                "https://music.apple.com/br/music-video/name/123"
            )
        ]
        self.assertIsInstance(video[0], MusicVideo)
        with self.assertRaises(SelectionRequiredError):
            _ = [
                x
                async for x in resolver.from_url(
                    "https://music.apple.com/br/artist/rick/669771"
                )
            ]
        with self.assertRaises(UnsupportedMediaError):
            _ = [
                x
                async for x in resolver.from_url(
                    "https://example.test/br/song/nope/123"
                )
            ]


if __name__ == "__main__":
    unittest.main()
