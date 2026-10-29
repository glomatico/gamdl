"""Native ID3 writing preserves MP3 audio and embeds Apple playback metadata."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from gamdl.download import Downloader, MediaDatabase, PathFormatter, Tags
from gamdl.media import Song, SongCodec
from gamdl.muxer import Muxer


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required"
)
class Mp3TaggingTests(unittest.TestCase):
    def test_write_id3_metadata_and_cover(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.mp3"
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=0.3",
                    "-c:a",
                    "libmp3lame",
                    str(path),
                ],
                check=True,
                capture_output=True,
            )
            tags = Tags(
                title="Title",
                artist="Artist",
                album="Album",
                album_artist="Album Artist",
                track_number=2,
                track_total=12,
                disc_number=1,
                disc_total=2,
                release_date="2004",
                genre=("Pop",),
                isrc="USAAA0000001",
            )
            Muxer().tag(path, tags=tags, cover_bytes=b"\xff\xd8\xff\xd9")
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"ID3\x04"))
            for marker in (
                b"TIT2",
                b"TPE1",
                b"TALB",
                b"TPE2",
                b"TRCK",
                b"TPOS",
                b"TCON",
                b"TSRC",
                b"APIC",
            ):
                self.assertIn(marker, raw)
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format_tags=title,artist,album,track,disc",
                    "-of",
                    "default=noprint_wrappers=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            for value in ("Title", "Artist", "Album", "2/12", "1/2"):
                self.assertIn(value, probe.stdout)


class UploadedMp3DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploaded_song_is_tagged_and_indexed_by_library_id(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp3"
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=0.3",
                    "-c:a",
                    "libmp3lame",
                    str(source),
                ],
                check=True,
                capture_output=True,
            )
            api = Mock()
            api.get_webplayback = AsyncMock(
                return_value={
                    "songList": [
                        {
                            "assets": [
                                {
                                    "URL": "https://example.com/song.mp3",
                                    "metadata": {
                                        "fileExtension": "mp3",
                                        "itemName": "Uploaded Title",
                                        "artistName": "Uploaded Artist",
                                        "playlistName": "Uploaded Album",
                                        "itemId": -1,
                                    },
                                }
                            ]
                        }
                    ]
                }
            )
            song = Song(
                "i.upload",
                api=api,
                is_library=True,
                metadata={"id": "i.upload", "relationships": {"catalog": {"data": []}}},
            )
            db = MediaDatabase(Path(directory) / "index.db")
            downloader = Downloader(
                path_formatter=PathFormatter(Path(directory) / "music"), database=db
            )
            downloader._read_track = AsyncMock(
                return_value=(source.read_bytes(), "none")
            )
            result = await downloader.download(
                song, codec=SongCodec.ALAC, ask=True, include_cover=False
            )
            self.assertEqual(result.path.suffix, ".mp3")
            self.assertEqual(db.get(result.path)["media_id"], "i.upload")
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format_tags=title,artist,album",
                    "-of",
                    "default=noprint_wrappers=1",
                    str(result.path),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            for value in ("Uploaded Title", "Uploaded Artist", "Uploaded Album"):
                self.assertIn(value, probe.stdout)


if __name__ == "__main__":
    unittest.main()
