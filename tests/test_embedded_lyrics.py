"""Regression: song lyrics are embedded by default unless excluded as a tag."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from gamdl.download import Downloader, FileStatus, MediaStatus, Tags
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

    async def test_independent_file_and_embedded_formats(self):
        xml = (
            '<tt><body><div><p begin="1s" end="2s">First line</p>'
            '<p begin="2.5s" end="4s">Second line</p></div></body></tt>'
        )
        self.song.get_lyrics.return_value = Lyrics.from_ttml(xml)
        output = self.root / "srt-embedded.m4a"
        result = await self.downloader.download(
            self.song,
            output,
            save_lyrics=True,
            lyrics_file_format="srt",
            embedded_lyrics_format="ttml",
        )
        self.assertEqual(result.lyrics.status, FileStatus.CREATED)
        self.assertEqual(result.lyrics.path, output.with_suffix(".srt"))
        self.assertIn("00:00:01,000 --> 00:00:02,000", result.lyrics.path.read_text())
        self.assertEqual(self.embedded_lyrics(output), xml)
        self.assertFalse(output.with_suffix(".lrc").exists())

        other = self.root / "text-external.m4a"
        await self.downloader.download(
            self.song,
            other,
            save_lyrics=True,
            lyrics_file_format="text",
            embedded_lyrics_format="srt",
        )
        self.assertEqual(
            other.with_suffix(".txt").read_text(), "First line\nSecond line"
        )
        self.assertIn("00:00:01,000 --> 00:00:02,000", self.embedded_lyrics(other))

        original = self.root / "ttml-external.m4a"
        await self.downloader.download(
            self.song,
            original,
            tag=False,
            save_lyrics=True,
            lyrics_file_format="ttml",
        )
        self.assertEqual(original.with_suffix(".ttml").read_text(), xml)

    async def test_external_format_unavailable_does_not_fallback(self):
        self.song.get_lyrics.return_value = Lyrics(stanzas=[[LyricsLine("No timing")]])
        for fmt in ("lrc", "enhanced-lrc", "srt", "ttml"):
            with self.subTest(fmt=fmt):
                output = self.root / f"unsynced-{fmt}.m4a"
                result = await self.downloader.download(
                    self.song,
                    output,
                    save_lyrics=True,
                    lyrics_file_format=fmt,
                    embedded_lyrics_format="srt",
                )
                self.assertEqual(result.lyrics.status, FileStatus.UNAVAILABLE)
                self.assertIsNone(result.lyrics.path)
                self.assertEqual(self.embedded_lyrics(output), "No timing")
                self.assertFalse(output.with_suffix(".txt").exists())
                self.assertFalse(output.with_suffix(".lrc").exists())
                self.assertFalse(output.with_suffix(".srt").exists())
                self.assertFalse(output.with_suffix(".ttml").exists())

    async def test_embedded_falls_back_to_line_lrc(self):
        output = self.root / "embedded-fallback.m4a"
        await self.downloader.download(self.song, output, embedded_lyrics_format="srt")
        self.assertEqual(
            self.embedded_lyrics(output),
            "[00:01.00]First line\n[00:02.50]Second line",
        )

    async def test_embedded_enhanced_lrc_and_bytes_api(self):
        xml = (
            '<tt><body><div><p begin="1s" end="2s">'
            '<span begin="1s" end="1.5s">First </span>'
            '<span begin="1.5s" end="2s">line</span>'
            "</p></div></body></tt>"
        )
        enhanced = Lyrics.from_ttml(xml, syllable=True)
        self.song.get_lyrics = AsyncMock(
            side_effect=lambda *, syllable=False: enhanced if syllable else self.lyrics
        )
        output = self.root / "enhanced-embedded.m4a"
        await self.downloader.download(
            self.song, output, embedded_lyrics_format="enhanced-lrc"
        )
        self.assertEqual(
            self.embedded_lyrics(output),
            "[00:01.00]<00:01.00>First <00:01.50>line",
        )
        self.song.get_lyrics.assert_any_await(syllable=True)
        self.downloader._read_track = AsyncMock(
            return_value=(self.source.read_bytes(), "none")
        )
        data = await self.downloader.get_bytes(self.song, embedded_lyrics_format="lrc")
        binary_output = self.root / "bytes-lrc.m4a"
        binary_output.write_bytes(data)
        self.assertEqual(
            self.embedded_lyrics(binary_output),
            "[00:01.00]First line\n[00:02.50]Second line",
        )

    async def test_enhanced_lrc_uses_syllable_endpoint_and_reuses_media(self):
        source = (
            '<tt><body><div><p begin="1s" end="2s">'
            '<span begin="1s" end="1.5s">First </span>'
            '<span begin="1.5s" end="2s">line</span>'
            "</p></div></body></tt>"
        )
        enhanced = Lyrics.from_ttml(source, syllable=True)
        self.song.get_lyrics = AsyncMock(
            side_effect=lambda *, syllable=False: enhanced if syllable else self.lyrics
        )
        output = self.root / "upgrade.m4a"
        await self.downloader.download(self.song, output, save_lyrics=True, tag=False)
        self.assertEqual(
            output.with_suffix(".lrc").read_text(),
            "[00:01.00]First line\n[00:02.50]Second line",
        )
        self.downloader._save_track.reset_mock()
        upgraded = await self.downloader.download(
            self.song,
            output,
            save_lyrics=True,
            tag=False,
            lyrics_file_format="enhanced-lrc",
        )
        self.assertEqual(upgraded.status, MediaStatus.SKIPPED)
        self.assertEqual(upgraded.lyrics.status, FileStatus.OVERWRITTEN)
        self.assertIn("<00:01.50>", output.with_suffix(".lrc").read_text())
        self.downloader._save_track.assert_not_awaited()
        self.song.get_lyrics.assert_any_await(syllable=True)

        downgraded = await self.downloader.download(
            self.song, output, save_lyrics=True, tag=False, lyrics_file_format="lrc"
        )
        self.assertEqual(downgraded.lyrics.status, FileStatus.OVERWRITTEN)
        self.assertEqual(
            output.with_suffix(".lrc").read_text(),
            "[00:01.00]First line\n[00:02.50]Second line",
        )
        self.downloader._save_track.assert_not_awaited()

    async def test_enhanced_without_syllables_uses_standard_lrc(self):
        self.song.get_lyrics = AsyncMock(
            side_effect=lambda *, syllable=False: None if syllable else self.lyrics
        )
        output = self.root / "enhanced-fallback.m4a"
        result = await self.downloader.download(
            self.song,
            output,
            save_lyrics=True,
            tag=False,
            lyrics_file_format="enhanced-lrc",
        )
        self.assertEqual(result.lyrics.status, FileStatus.CREATED)
        self.assertEqual(
            output.with_suffix(".lrc").read_text(),
            "[00:01.00]First line\n[00:02.50]Second line",
        )
        self.song.get_lyrics.assert_any_await(syllable=True)
        self.song.get_lyrics.assert_any_await()

    async def test_requested_extension_does_not_skip_other_sidecar(self):
        output = self.root / "switch-type.m4a"
        await self.downloader.download(self.song, output, save_lyrics=True)
        self.assertTrue(output.with_suffix(".lrc").exists())
        result = await self.downloader.download(
            self.song,
            output,
            save_lyrics=True,
            lyrics_file_format="text",
        )
        self.assertEqual(result.status, MediaStatus.SKIPPED)
        self.assertEqual(result.lyrics.status, FileStatus.CREATED)
        self.assertEqual(
            output.with_suffix(".txt").read_text(), "First line\nSecond line"
        )

    async def test_format_arguments_are_validated(self):
        with self.assertRaises(ValueError):
            await self.downloader.download(
                self.song, self.root / "wrong.m4a", lyrics_file_format="vtt"
            )
        with self.assertRaises(ValueError):
            await self.downloader.get_bytes(self.song, embedded_lyrics_format="vtt")


if __name__ == "__main__":
    unittest.main()
