"""Opt-in live checks of the Media/Song layer (no downloads or licenses).

GAMDL_RUN_LIVE=1 uv run python -m unittest discover -s tests/live -p test_media.py -v
"""

from __future__ import annotations

import os
import unittest

from gamdl.media import GamdlMediaError, Lyrics, MediaResolver, Song
from tests.live_auth import LiveSettings, open_live_session

ALBUM_ID = "1559885420"
SONG_ID = "1559885421"


@unittest.skipUnless(
    os.environ.get("GAMDL_RUN_LIVE") == "1",
    "Opt-in only: set GAMDL_RUN_LIVE=1 to contact Apple Music",
)
class MediaLiveTests(unittest.IsolatedAsyncioTestCase):
    """Read-only Rick Astley checks using the configured wrapper-first auth."""

    async def asyncSetUp(self) -> None:
        self.context = open_live_session(LiveSettings.from_env(), interval=0.75)
        self.session = await self.context.__aenter__()
        self.addAsyncCleanup(self.context.__aexit__, None, None, None)

    async def test_song_metadata_cover_and_tags(self) -> None:
        song = Song.from_id(SONG_ID, api=self.session.api)
        metadata = await song.get_metadata()
        self.assertEqual(metadata["id"], SONG_ID)
        tags = await song.get_tags()
        self.assertIn("Never Gonna Give You Up", tags.title)
        self.assertIn("Whenever You Need Somebody", tags.album)
        self.assertEqual(tags.track_number, 1)
        self.assertEqual(tags.album_id, int(ALBUM_ID))
        self.assertEqual(tags.genre, ("Pop",))
        cover = await song.get_cover()
        self.assertIsNotNone(cover)
        self.assertTrue(cover.get_formatted_url(width=600, height=600))

    async def test_standard_and_syllable_lyrics(self) -> None:
        song = Song.from_id(SONG_ID, api=self.session.api)
        lyrics = await song.get_lyrics()
        syllable = await song.get_lyrics(syllable=True)
        self.assertIsInstance(lyrics, Lyrics)
        self.assertIsInstance(syllable, Lyrics)
        self.assertGreater(len(lyrics.lines), 0)
        self.assertTrue(lyrics.is_synced)
        self.assertTrue(syllable.is_syllable_synced)
        self.assertFalse(lyrics.syllable)
        self.assertTrue(syllable.syllable)
        self.assertTrue(syllable.to_lrc())
        self.assertIn("<", syllable.to_lrc())
        self.assertNotIn("<", lyrics.to_lrc())
        self.assertTrue(
            all(
                "".join(segment.text for segment in line.segments).strip() == line.text
                for line in syllable.lines
                if line.segments
            )
        )
        with self.assertRaisesRegex(GamdlMediaError, "only export"):
            syllable.to_plain_text()
        with self.assertRaisesRegex(GamdlMediaError, "only export"):
            syllable.to_srt()
        self.assertEqual(syllable.to_ttml(), syllable.source_ttml)

    async def test_album_resolver_and_seeded_metadata(self) -> None:
        resolver = MediaResolver(self.session.api)
        tracks = []
        async for media in resolver.from_album(ALBUM_ID):
            self.assertIsInstance(media, Song)
            tracks.append(media)
            if len(tracks) == 2:
                break
        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].id, SONG_ID)
        self.assertEqual(tracks[0].source.index, 0)
        self.assertEqual((await tracks[0].get_metadata())["id"], SONG_ID)


if __name__ == "__main__":
    unittest.main()
