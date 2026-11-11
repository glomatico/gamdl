"""Rich per-operation results and UI-neutral downloader stage events."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, Mock

from gamdl.download import (
    DatabaseStatus,
    DownloadFinalizationError,
    DownloadStage,
    EventState,
    FileStatus,
    MediaDatabase,
    MediaStatus,
    PathFormatter,
    PlaylistStatus,
    PlaylistTags,
)
from gamdl.media import Song
from gamdl.media.song.lyrics import Lyrics, LyricsLine
from tests import test_downloader as downloader_fixtures
from tests.test_media import SONG_DATA, fake_api
from tests.test_muxer import JPEG


class RichDownloadResultTests(unittest.IsolatedAsyncioTestCase):
    setUp = downloader_fixtures.DownloaderTests.setUp
    song = downloader_fixtures.DownloaderTests.song

    def downloader(self):
        downloader, tags = downloader_fixtures.DownloaderTests.downloader(self)
        mocked = downloader._read_track.side_effect

        async def read(media_id, source, track, progress_callback, **_):
            return await mocked(media_id, source, track, progress_callback)

        downloader._read_track.side_effect = read
        return downloader, tags

    async def test_results_track_cover_lyrics_playlist_and_database(self):
        downloader, _ = self.downloader()
        output_dir = self.directory / "Music"
        downloader.path_formatter = PathFormatter(
            output_dir,
            playlist_folder_template="Lists/{playlist_artist}",
            playlist_file_template="{playlist_title} ({playlist_id})",
        )
        downloader.database = MediaDatabase(self.directory / "index.sqlite3")
        song = self.song()
        cover = Mock(get_cover_bytes=AsyncMock(return_value=JPEG))
        song.get_cover.return_value = cover
        song.get_lyrics = AsyncMock(
            return_value=Lyrics(stanzas=[[LyricsLine("hello", start=1000)]])
        )
        playlist = PlaylistTags(
            title=".My Playlist", artist=".DJ", playlist_id="p.12", track=2
        )
        events = []
        result = await downloader.download(
            song,
            save_cover=True,
            save_lyrics=True,
            save_playlist=True,
            playlist_tags=playlist,
            event_callback=events.append,
        )
        self.assertEqual(result.status, MediaStatus.DOWNLOADED)
        self.assertEqual(result.cover.status, FileStatus.CREATED)
        self.assertEqual(result.lyrics.status, FileStatus.CREATED)
        self.assertEqual(result.database.status, DatabaseStatus.INSERTED)
        self.assertEqual(result.playlist_file.status, PlaylistStatus.UPDATED)
        self.assertEqual(result.cover.path, result.path.with_suffix(".jpg"))
        self.assertEqual(result.lyrics.path, result.path.with_suffix(".lrc"))
        expected = output_dir / "Lists/_DJ/_My Playlist (p.12).m3u8"
        self.assertEqual(result.playlist_file.path, expected)
        lines = expected.read_text().splitlines()
        self.assertEqual(
            lines[:4],
            [
                "#EXTM3U",
                "#PLAYLIST:.My Playlist",
                "#Gamdl-PLAYLIST-ARTIST:.DJ",
                "#Gamdl-APPLE-MUSIC-ID:p.12",
            ],
        )
        self.assertEqual(lines[4], "")
        self.assertTrue(lines[5].endswith("/Sample Track.m4a"))
        self.assertEqual(events[-1].stage, DownloadStage.COMPLETED)
        self.assertIs(events[-1].result, result)
        self.assertEqual(events[-1].state, EventState.COMPLETED)
        self.assertEqual(
            [
                event.state
                for event in events
                if event.stage == DownloadStage.SAVING_COVER
            ],
            [EventState.STARTED, EventState.COMPLETED],
        )
        with self.assertRaises(FrozenInstanceError):
            result.status = MediaStatus.SKIPPED

        events.clear()
        downloader._read_track.reset_mock()
        skipped = await downloader.download(
            song,
            save_cover=True,
            save_lyrics=True,
            save_playlist=True,
            playlist_tags=playlist,
            event_callback=events.append,
        )
        self.assertEqual(skipped.status, MediaStatus.SKIPPED)
        self.assertEqual(skipped.cover.status, FileStatus.SKIPPED)
        self.assertEqual(skipped.lyrics.status, FileStatus.SKIPPED)
        self.assertEqual(skipped.database.status, DatabaseStatus.UNCHANGED)
        self.assertEqual(skipped.playlist_file.status, PlaylistStatus.UNCHANGED)
        downloader._read_track.assert_not_awaited()
        self.assertFalse(any(ev.stage == DownloadStage.DOWNLOADING for ev in events))

        overwritten = await downloader.download(
            song,
            save_cover=True,
            save_lyrics=True,
            save_playlist=True,
            playlist_tags=playlist,
            overwrite=True,
        )
        self.assertEqual(overwritten.status, MediaStatus.OVERWRITTEN)
        self.assertEqual(overwritten.cover.status, FileStatus.OVERWRITTEN)
        self.assertEqual(overwritten.lyrics.status, FileStatus.OVERWRITTEN)
        self.assertEqual(overwritten.playlist_file.status, PlaylistStatus.UNCHANGED)

    async def test_unavailable_sidecars_and_explicit_playlist_overrides_formatter(self):
        downloader, _ = self.downloader()
        downloader.path_formatter = PathFormatter(self.directory / "Auto")
        song = self.song()
        song.get_lyrics = AsyncMock(return_value=None)
        explicit = self.directory / "Custom" / "other.m3u8"
        result = await downloader.download(
            song,
            save_cover=True,
            save_lyrics=True,
            save_playlist=True,
            playlist_tags=PlaylistTags(title="Ignored", artist="None", track=1),
            playlist_path=explicit,
        )
        self.assertEqual(result.cover.status, FileStatus.UNAVAILABLE)
        self.assertIsNone(result.cover.path)
        self.assertEqual(result.lyrics.status, FileStatus.UNAVAILABLE)
        self.assertIsNone(result.lyrics.path)
        self.assertEqual(result.playlist_file.path, explicit)
        self.assertEqual(result.playlist_file.status, PlaylistStatus.UPDATED)
        self.assertIsNone(result.database)

    async def test_missing_apple_lyrics_is_successful_media_with_unavailable_sidecar(
        self,
    ):
        downloader, _ = self.downloader()
        api = fake_api()
        song = Song.from_metadata(
            {
                **SONG_DATA,
                "attributes": {**SONG_DATA["attributes"], "hasLyrics": False},
            },
            api=api,
        )
        target = self.directory / "already-downloaded.m4a"
        target.write_bytes(b"existing downloaded audio")
        result = await downloader.download(song, target, save_lyrics=True)
        self.assertEqual(result.status, MediaStatus.SKIPPED)
        self.assertEqual(result.lyrics.status, FileStatus.UNAVAILABLE)
        self.assertIsNone(result.lyrics.path)
        self.assertEqual(target.read_bytes(), b"existing downloaded audio")
        self.assertFalse(target.with_suffix(".lrc").exists())
        api.get_lyrics.assert_not_awaited()
        downloader._read_track.assert_not_awaited()

    async def test_long_title_retry_writes_cover_and_lyrics_without_media_download(
        self,
    ):
        """Repair finalization for a filename truncated to 255 UTF-8 bytes."""
        downloader, _ = self.downloader()
        formatter = PathFormatter(self.directory / "Apple Music")
        song = self.song()
        filename = formatter.get_media_path(
            song,
            downloader_fixtures.Tags(
                title="We Are The World (feat. " + "artist, " * 80,
                artist="U.S.A. for Africa",
                album="We Are The World (Remastered)",
                album_artist="U.S.A. for Africa",
                track_number=1,
            ),
            extension=".m4a",
        )
        self.assertEqual(len(filename.name.encode("utf-8")), 255)
        filename.parent.mkdir(parents=True)
        filename.write_bytes(b"existing media bytes")
        song.get_cover.return_value = Mock(get_cover_bytes=AsyncMock(return_value=JPEG))
        song.get_lyrics = AsyncMock(
            return_value=Lyrics(stanzas=[[LyricsLine("Hello world", start=1000)]])
        )

        outcome = await downloader.download(
            song, filename, overwrite=False, save_cover=True, save_lyrics=True
        )
        self.assertEqual(outcome.status, MediaStatus.SKIPPED)
        self.assertEqual(outcome.cover.status, FileStatus.CREATED)
        self.assertEqual(outcome.lyrics.status, FileStatus.CREATED)
        self.assertTrue(filename.with_suffix(".jpg").is_file())
        self.assertTrue(filename.with_suffix(".lrc").is_file())
        self.assertEqual(filename.read_bytes(), b"existing media bytes")
        self.assertFalse(list(filename.parent.glob(".gamdl-*")))
        downloader._read_track.assert_not_awaited()

    async def test_sidecar_failure_includes_previous_partial_result(self):
        downloader, _ = self.downloader()
        song = self.song()
        song.get_cover.return_value = Mock(get_cover_bytes=AsyncMock(return_value=JPEG))
        song.get_lyrics = AsyncMock(side_effect=OSError("lyrics unavailable"))
        target = self.directory / "finished.m4a"
        events = []
        with self.assertRaises(DownloadFinalizationError) as failure:
            await downloader.download(
                song,
                target,
                save_cover=True,
                save_lyrics=True,
                exclude_tags=("lyrics",),
                event_callback=events.append,
            )
        error = failure.exception
        self.assertEqual(error.stage, "lyrics")
        self.assertEqual(error.partial_result.status, MediaStatus.DOWNLOADED)
        self.assertEqual(error.partial_result.cover.status, FileStatus.CREATED)
        self.assertIsNone(error.partial_result.lyrics)
        self.assertTrue(target.exists())
        self.assertTrue(target.with_suffix(".jpg").exists())
        self.assertEqual(
            [ev.state for ev in events if ev.stage == DownloadStage.SAVING_LYRICS],
            [EventState.STARTED, EventState.FAILED],
        )

    async def test_cover_finalization_identifies_its_own_stage_and_cause(self):
        downloader, _ = self.downloader()
        song = self.song()
        song.get_cover.side_effect = PermissionError("cannot read cover")
        target = self.directory / "ready.m4a"
        target.write_bytes(b"already published")
        with self.assertRaises(DownloadFinalizationError) as raised:
            await downloader.download(song, target, save_cover=True)
        self.assertEqual(raised.exception.stage, "cover")
        self.assertIsInstance(raised.exception.__cause__, PermissionError)
        self.assertEqual(raised.exception.partial_result.status, "skipped")
        self.assertEqual(target.read_bytes(), b"already published")

    async def test_skip_does_not_emit_transfer_stages_and_get_bytes_no_side_effects(
        self,
    ):
        downloader, _ = self.downloader()
        target = self.directory / "song.m4a"
        events = []
        await downloader.download(self.song(), target)
        await downloader.download(self.song(), target, event_callback=events.append)
        self.assertEqual([e.stage for e in events], [DownloadStage.COMPLETED])

        events.clear()
        await downloader.get_bytes(self.song(), event_callback=events.append)
        self.assertEqual(events[-1].stage, DownloadStage.COMPLETED)
        self.assertIsNone(events[-1].result)
        self.assertTrue(any(e.stage == DownloadStage.FETCHING_STREAMS for e in events))
        self.assertTrue(any(e.stage == DownloadStage.MUXING for e in events))
        self.assertTrue(any(e.stage == DownloadStage.TAGGING for e in events))
