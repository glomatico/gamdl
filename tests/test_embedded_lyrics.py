"""Regression: song lyrics are embedded by default unless excluded as a tag."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from gamdl.download import Downloader, Tags
from gamdl.media import Song, SongCodec
from gamdl.media.song.lyrics import Lyrics, LyricsLine
from tests.test_muxer import mp4_tag_atoms


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required for audio fixture")
class EmbeddedLyricsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.m4a"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=0.2",
                "-c:a",
                "aac",
                "-movflags",
                "+frag_keyframe+empty_moov+default_base_moof",
                str(self.source),
            ],
            check=True,
            capture_output=True,
        )
        self.lyrics = Lyrics(
            stanzas=[
                [
                    LyricsLine("First line", start=1000),
                    LyricsLine("Second line", start=2500),
                ]
            ]
        )
        self.song = Song.from_id("12345", api=Mock())
        self.song.get_stream = AsyncMock(return_value=Mock(codec=SongCodec.AAC))
        self.song.get_cover = AsyncMock(return_value=None)
        self.song.get_lyrics = AsyncMock(return_value=self.lyrics)
        resolver = Mock()
        resolver.get_tags = AsyncMock(return_value=Tags(title="Test", title_id=12345))
        self.downloader = Downloader(tag_resolver=resolver)

        async def save(
            _id, _source, _track, destination, _callback, *, event_callback=None
        ):
            shutil.copyfile(self.source, destination)
            return "none"

        self.downloader._save_track = AsyncMock(side_effect=save)

    def embedded_lyrics(self, path: Path) -> str | None:
        atoms = mp4_tag_atoms(path)
        return atoms[b"\xa9lyr"][0][1].decode("utf-8") if b"\xa9lyr" in atoms else None

    async def test_save_lyrics_embeds_plain_text_and_saves_timed_sidecar(self):
        output = self.root / "song.m4a"
        await self.downloader.download(self.song, output, save_lyrics=True)
        self.assertEqual(self.embedded_lyrics(output), "First line\nSecond line")
        self.assertEqual(
            output.with_suffix(".lrc").read_text(),
            "[00:01.00]First line\n[00:02.50]Second line",
        )
        self.song.get_lyrics.assert_awaited()

    async def test_excluded_lyrics_tag_still_allows_sidecar(self):
        output = self.root / "excluded.m4a"
        await self.downloader.download(
            self.song, output, save_lyrics=True, exclude_tags=("lyrics",)
        )
        self.assertIsNone(self.embedded_lyrics(output))
        self.assertTrue(output.with_suffix(".lrc").is_file())

    async def test_tag_disabled_keeps_sidecar(self):
        output = self.root / "sidecar-only.m4a"
        await self.downloader.download(self.song, output, save_lyrics=True, tag=False)
        self.assertIsNone(self.embedded_lyrics(output))
        self.assertTrue(output.with_suffix(".lrc").is_file())

    async def test_sidecars_disabled_still_embeds_lyrics(self):
        output = self.root / "embedded-only.m4a"
        await self.downloader.download(self.song, output, save_lyrics=False)
        self.assertEqual(self.embedded_lyrics(output), "First line\nSecond line")
        self.assertFalse(output.with_suffix(".lrc").exists())
        self.song.get_lyrics.assert_awaited()

    async def test_tag_exclusion_without_sidecar_does_not_fetch_lyrics(self):
        output = self.root / "no-lyrics.m4a"
        await self.downloader.download(
            self.song, output, save_lyrics=False, exclude_tags=("lyrics",)
        )
        self.assertIsNone(self.embedded_lyrics(output))
        self.assertFalse(output.with_suffix(".lrc").exists())
        self.song.get_lyrics.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
