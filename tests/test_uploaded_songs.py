"""Unmatched library songs are downloaded as untouched webplayback files."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from gamdl.download import Downloader, MediaDatabase, PathFormatter
from gamdl.media import Song, SongCodec
from gamdl.media.stream import HttpStream


class UploadedSongTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.api = Mock()
        self.api.get_webplayback = AsyncMock(
            return_value={
                "songList": [
                    {
                        "assets": [
                            {
                                "URL": "https://example.com/audio",
                                "metadata": {
                                    "fileExtension": "mp3",
                                    "itemName": "Original Upload",
                                    "artistName": "Artist",
                                    "playlistName": "Album",
                                    "trackNumber": 1,
                                },
                            }
                        ]
                    }
                ]
            }
        )
        self.song = Song(
            "i.uploaded",
            api=self.api,
            is_library=True,
            metadata={"id": "i.uploaded", "relationships": {"catalog": {"data": []}}},
        )

    async def test_uploaded_stream_ignores_codec_and_selector(self):
        selector = AsyncMock(side_effect=AssertionError("selector must not run"))
        stream = await self.song.get_stream(
            codec=SongCodec.ALAC, ask=True, selector=selector
        )
        self.assertIsInstance(stream, HttpStream)
        self.assertEqual(stream.url, "https://example.com/audio")
        self.assertEqual(len(await self.song.get_streams()), 1)
        selector.assert_not_awaited()
        self.api.get_webplayback.assert_awaited_once()

    async def test_download_preserves_bytes_extension_and_database_id(self):
        db = MediaDatabase(self.directory / "media.sqlite3")
        downloader = Downloader(
            database=db, path_formatter=PathFormatter(self.directory / "music")
        )
        raw = b"ID3\x02\x00\x00original mp3 bytes"
        downloader._read_track = AsyncMock(return_value=(raw, "none"))
        downloader.muxer.remux = Mock(side_effect=AssertionError("must not remux"))
        downloader.muxer.tag = Mock(side_effect=AssertionError("must not tag"))
        result = await downloader.download(self.song, codec=SongCodec.ALAC, ask=True)
        self.assertEqual(result.path.suffix, ".mp3")
        self.assertEqual(result.path.read_bytes(), raw)
        self.assertEqual(result.codec, None)
        self.assertEqual(db.get(result.path)["media_id"], "i.uploaded")
        self.assertEqual((await downloader.download(self.song)).status, "skipped")
        downloader._read_track.assert_awaited_once()

    async def test_rejects_multiple_assets(self):
        self.api.get_webplayback.return_value["songList"][0]["assets"].append(
            {"URL": "https://example.com/other"}
        )
        with self.assertRaisesRegex(Exception, "single direct asset"):
            await self.song.get_uploaded_stream()


if __name__ == "__main__":
    unittest.main()
