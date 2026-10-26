"""Offline integration tests for the reusable one-item download workflow."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from gamdl.download import (
    DownloadFinalizationError,
    Downloader,
    DownloadProgress,
    MediaDatabase,
    PathFormatter,
    Tags,
)
from gamdl.media import (
    MediaSource,
    MusicVideo,
    MusicVideoCodec,
    Song,
    SongCodec,
    UploadedVideo,
    UploadedVideoQuality,
)
from gamdl.media.song.lyrics import Lyrics, LyricsLine
from gamdl.media.music_video.stream import MusicVideoStreams
from gamdl.stream import StreamProgress
from tests.test_muxer import JPEG, PNG, brands, mp4_tag_atoms, track_hashes


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and FFprobe required for downloader integration fixtures",
)
class DownloaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.audio = self.directory / "audio.m4a"
        self.video = self.directory / "video.mp4"
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
                "sine=frequency=220:sample_rate=48000:duration=0.3",
                "-c:a",
                "aac",
                "-movflags",
                "+frag_keyframe+empty_moov+default_base_moof",
                str(self.audio),
            ],
            check=True,
            capture_output=True,
        )
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
                "testsrc2=duration=0.3:size=160x96:rate=25",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+frag_keyframe+empty_moov+default_base_moof",
                str(self.video),
            ],
            check=True,
            capture_output=True,
        )

    def song(self):
        media = Song.from_id("111", api=Mock())
        media.get_stream = AsyncMock(return_value=Mock(codec=SongCodec.AAC))
        media.get_cover = AsyncMock(return_value=None)
        return media

    def music_video(self):
        media = MusicVideo.from_id("222", api=Mock())
        media.get_streams = AsyncMock(
            return_value=MusicVideoStreams(
                video=Mock(codec=MusicVideoCodec.H264),
                audio=Mock(codec=SongCodec.AAC),
            )
        )
        media.get_cover = AsyncMock(return_value=None)
        return media

    def downloader(self):
        tag_resolver = Mock()
        tag_resolver.get_tags = AsyncMock(
            return_value=Tags(
                title="Sample Track", artist="Sample Artist", title_id=111
            )
        )
        downloader = Downloader(tag_resolver=tag_resolver)

        async def read(_media_id, _source, track, _callback):
            return (
                (self.video if track == "video" else self.audio).read_bytes(),
                "none",
            )

        downloader._read_track = AsyncMock(side_effect=read)
        return downloader, tag_resolver

    async def test_song_bytes_remux_tags_and_optional_lyrics(self):
        downloader, tags = self.downloader()
        song = self.song()
        song.get_lyrics = AsyncMock(
            return_value=Mock(to_plain_text=lambda: "These are sample lyrics")
        )
        value = await downloader.get_bytes(
            song,
            codec_priority=[SongCodec.ALAC, SongCodec.AAC],
            include_lyrics=True,
        )
        finished = self.directory / "tagged.m4a"
        finished.write_bytes(value)
        self.assertEqual(brands(finished)[0], b"M4A ")
        self.assertEqual(track_hashes(self.audio, "a"), track_hashes(finished, "a"))
        atoms = mp4_tag_atoms(finished)
        self.assertIn(b"\xa9nam", atoms)
        self.assertIn(b"\xa9lyr", atoms)
        song.get_stream.assert_awaited_once()
        self.assertEqual(
            song.get_stream.call_args.kwargs["codec_priority"],
            [SongCodec.ALAC, SongCodec.AAC],
        )
        tags.get_tags.assert_awaited_once_with(song)

    async def test_atomic_download_skip_replace_and_error(self):
        downloader, tags = self.downloader()
        song = self.song()
        output = self.directory / "subdir" / "song.m4a"
        output.parent.mkdir()
        output.write_bytes(b"my existing file")
        result = await downloader.download(song, output)
        self.assertEqual(result.status, "skipped")
        self.assertEqual(output.read_bytes(), b"my existing file")
        downloader._read_track.assert_not_awaited()
        tags.get_tags.assert_not_awaited()
        with self.assertRaises(FileExistsError):
            await downloader.download(song, output, overwrite="error")
        result = await downloader.download(song, output, overwrite="replace")
        self.assertEqual(result.status, "downloaded")
        self.assertEqual(result.codec, SongCodec.AAC)
        self.assertEqual(result.decryption_methods, ("none",))
        self.assertEqual(result.bytes_written, output.stat().st_size)
        self.assertEqual(brands(output)[0], b"M4A ")
        self.assertEqual(list(output.parent.glob(".*.part")), [])

    async def test_video_mp4_with_audio_mux_and_codec_priority(self):
        downloader, _ = self.downloader()
        video = self.music_video()
        output = self.directory / "movie.mp4"
        result = await downloader.download(
            video,
            output,
            codec_priority=[MusicVideoCodec.H265, MusicVideoCodec.H264],
            max_height=720,
        )
        self.assertEqual(result.status, "downloaded")
        self.assertEqual(result.codec, MusicVideoCodec.H264)
        self.assertEqual(result.decryption_methods, ("none", "none"))
        self.assertEqual(brands(output), (b"mp42", [b"mp42", b"isom"]))
        self.assertEqual(track_hashes(self.video, "v"), track_hashes(output, "v"))
        self.assertEqual(track_hashes(self.audio, "a"), track_hashes(output, "a"))
        self.assertEqual(
            video.get_streams.call_args.kwargs["codec_priority"],
            [MusicVideoCodec.H265, MusicVideoCodec.H264],
        )
        self.assertEqual(video.get_streams.call_args.kwargs["max_height"], 720)
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-xerror",
                "-v",
                "error",
                "-i",
                str(output),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            check=True,
        )

    async def test_video_get_bytes_defaults_to_m4v_and_supports_ask(self):
        downloader, _ = self.downloader()
        video = self.music_video()
        callback = Mock(return_value=0)
        data = await downloader.get_bytes(video, ask=True, selector=callback, tag=False)
        self.assertEqual(data[8:12], b"M4V ")
        self.assertTrue(video.get_streams.call_args.kwargs["ask"])
        self.assertIs(video.get_streams.call_args.kwargs["selector"], callback)

    async def test_video_download_preserves_large_catalog_id_during_tagging(self):
        """Regression: video 6805195215 used to fail after both tracks downloaded."""
        downloader, tag_resolver = self.downloader()
        tag_resolver.get_tags.return_value = Tags(
            title="Say Say Say", title_id=6805195215, media_type=6
        )
        destination = self.directory / "say-say-say.m4v"
        result = await downloader.download(self.music_video(), destination)
        self.assertEqual(result.status, "downloaded")
        self.assertEqual(
            mp4_tag_atoms(destination)[b"cnID"],
            [(21, (6805195215).to_bytes(8, "big"))],
        )
        self.assertEqual(brands(destination)[0], b"M4V ")
        self.assertEqual(track_hashes(self.video, "v"), track_hashes(destination, "v"))
        self.assertEqual(track_hashes(self.audio, "a"), track_hashes(destination, "a"))

    async def test_failed_video_track_cancels_sibling_and_cleans_publication(self):
        downloader, _ = self.downloader()
        video = self.music_video()
        audio_started = asyncio.Event()
        cancelled = asyncio.Event()

        async def fail_one(_media_id, _source, track, _callback):
            if track == "video":
                await audio_started.wait()
                raise RuntimeError("video failed")
            audio_started.set()
            try:
                await asyncio.sleep(3600)
            finally:
                cancelled.set()

        downloader._read_track = AsyncMock(side_effect=fail_one)
        output = self.directory / "failed.m4v"
        with self.assertRaisesRegex(RuntimeError, "video failed"):
            await downloader.download(video, output)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(output.exists())
        self.assertEqual(list(self.directory.glob(".failed*")), [])

    async def test_publish_errors_do_not_overwrite_existing_file(self):
        downloader, _ = self.downloader()
        song = self.song()
        output = self.directory / "existing.m4a"
        output.write_bytes(b"keep")
        with patch("gamdl.download.publication.os.link", side_effect=FileExistsError):
            result = await downloader.download(song, output, overwrite="skip")
        self.assertEqual(result.status, "skipped")
        self.assertEqual(output.read_bytes(), b"keep")
        self.assertEqual(list(self.directory.glob(".existing*")), [])

    async def test_progress_callback_contains_media_track_and_stream_snapshot(self):
        sample = self.audio.read_bytes()

        class FakeStreamer:
            decryption_method = "test"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def __aiter__(self):
                yield sample[:99]
                yield sample[99:]

            @property
            def progress(self):
                return StreamProgress(100, 100, 1, 2, 200, 0.5)

        progress = []
        with patch(
            "gamdl.download.downloader.Streamer", return_value=FakeStreamer()
        ) as streamer_factory:
            downloader = Downloader()
            data, method = await downloader._read_track(
                "111", Mock(), "audio", progress.append
            )
        self.assertEqual(data, sample)
        self.assertEqual(method, "test")
        self.assertEqual(len(progress), 2)
        self.assertTrue(all(isinstance(p, DownloadProgress) for p in progress))
        self.assertEqual({p.media_id for p in progress}, {"111"})
        self.assertEqual({p.track for p in progress}, {"audio"})
        self.assertEqual(progress[0].stream.completed_parts, 1)
        streamer_factory.assert_called_once()

    async def test_external_cancellation_closes_both_video_tasks(self):
        downloader, _ = self.downloader()
        video = self.music_video()
        both_started = asyncio.Event()
        both_cancelled = set()
        starts = set()

        async def slow_track(_id, _source, track, _callback):
            starts.add(track)
            if len(starts) == 2:
                both_started.set()
            try:
                await asyncio.sleep(3600)
            finally:
                both_cancelled.add(track)

        downloader._read_track = AsyncMock(side_effect=slow_track)
        output = self.directory / "cancelled.mp4"
        running = asyncio.create_task(downloader.download(video, output))
        await asyncio.wait_for(both_started.wait(), timeout=2)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(both_cancelled, {"audio", "video"})
        self.assertFalse(output.exists())
        self.assertEqual(list(self.directory.glob(".cancelled*")), [])

    async def test_option_validation(self):
        downloader, _ = self.downloader()
        song = self.song()
        video = self.music_video()
        with self.assertRaises(ValueError):
            await downloader.download(song, self.directory / "invalid.mp4")
        with self.assertRaises(ValueError):
            await downloader.download(video, self.directory / "invalid.m4a")
        with self.assertRaises(ValueError):
            await downloader.download(
                song, self.directory / "valid.m4a", overwrite="rename"
            )
        with self.assertRaises(ValueError):
            await downloader.get_bytes(song, video_codec=MusicVideoCodec.H264)
        with self.assertRaises(ValueError):
            await downloader.get_bytes(video, codec=SongCodec.AAC)
        with self.assertRaises(ValueError):
            await downloader.get_bytes(video, container="avi")
        with self.assertRaises(ValueError):
            Downloader(concurrency=0)

    async def test_published_song_sidecars_index_playlist_and_retry_on_skip(self):
        downloader, _ = self.downloader()
        db = MediaDatabase(self.directory / "db" / "media.sqlite3")
        downloader.database = db
        song = self.song()
        song.source = MediaSource("playlist_tracks", "playlist-id", 1)
        cover = Mock(get_cover_bytes=AsyncMock(return_value=JPEG))
        song.get_cover.return_value = cover
        lyrics = Lyrics(stanzas=[[LyricsLine("Sample synced line", start=2000)]])
        song.get_lyrics = AsyncMock(return_value=lyrics)
        output = self.directory / "music" / "song.m4a"
        playlist = self.directory / "playlists" / "tracks.m3u8"
        result = await downloader.download(
            song,
            output,
            include_cover=True,
            save_cover=True,
            save_lyrics=True,
            playlist_path=playlist,
        )
        self.assertEqual(result.status, "downloaded")
        self.assertEqual(output.with_suffix(".jpg").read_bytes(), JPEG)
        self.assertEqual(
            output.with_suffix(".lrc").read_text(), "[00:02.00]Sample synced line"
        )
        self.assertEqual(playlist.read_text(), "#EXTM3U\n\n../music/song.m4a\n")
        self.assertEqual(db.get(output)["codec"], SongCodec.AAC.value)
        self.assertEqual(db.get(output)["source_id"], "playlist-id")
        song.get_cover.assert_awaited_once()  # embedded art reused for sidecar

        output.with_suffix(".jpg").unlink()
        output.with_suffix(".lrc").unlink()
        playlist.unlink()
        db.path.unlink()
        downloader._read_track.reset_mock()
        song.get_stream.reset_mock()
        retry = await downloader.download(
            song, output, save_cover=True, save_lyrics=True, playlist_path=playlist
        )
        self.assertEqual(retry.status, "skipped")
        downloader._read_track.assert_not_awaited()
        song.get_stream.assert_not_awaited()
        self.assertEqual(output.with_suffix(".jpg").read_bytes(), JPEG)
        self.assertTrue(output.with_suffix(".lrc").exists())
        self.assertTrue(playlist.exists())
        self.assertEqual(db.get(output)["media_id"], "111")
        # Further retries don't duplicate entries or rewrite existing sidecars.
        self.assertEqual(
            (await downloader.download(song, output, playlist_path=playlist)).status,
            "skipped",
        )
        self.assertEqual(playlist.read_text(), "#EXTM3U\n\n../music/song.m4a\n")

    async def test_unsynced_song_lyrics_use_txt_and_png_cover(self):
        downloader, _ = self.downloader()
        song = self.song()
        song.get_lyrics = AsyncMock(
            return_value=Lyrics(stanzas=[[LyricsLine("Hello")]])
        )
        song.get_cover.return_value = Mock(get_cover_bytes=AsyncMock(return_value=PNG))
        output = self.directory / "unsynced.m4a"
        await downloader.download(
            song, output, tag=False, save_cover=True, save_lyrics=True
        )
        self.assertEqual(output.with_suffix(".txt").read_text(), "Hello")
        self.assertEqual(output.with_suffix(".png").read_bytes(), PNG)
        self.assertFalse(output.with_suffix(".lrc").exists())

    async def test_video_sidecar_and_explicit_playlist_position(self):
        downloader, _ = self.downloader()
        video = self.music_video()
        video.get_cover.return_value = Mock(get_cover_bytes=AsyncMock(return_value=PNG))
        output = self.directory / "album" / "video.m4v"
        playlist = self.directory / "collection.m3u8"
        result = await downloader.download(
            video,
            output,
            save_cover=True,
            playlist_path=playlist,
            playlist_index=2,
        )
        self.assertEqual(result.status, "downloaded")
        self.assertEqual(output.with_suffix(".png").read_bytes(), PNG)
        self.assertEqual(playlist.read_text(), "#EXTM3U\n\n\nalbum/video.m4v\n")
        with self.assertRaises(ValueError):
            await downloader.download(video, output, save_lyrics=True)
        with self.assertRaises(ValueError):
            await downloader.download(video, output, playlist_index=1)
        with self.assertRaises(ValueError):
            await downloader.download(
                video, output, playlist_path=playlist, playlist_index=-1
            )

    async def test_failed_finalization_keeps_file_then_skip_retries(self):
        downloader, _ = self.downloader()
        song = self.song()
        destination = self.directory / "repair.m4a"
        failure = Mock(record=Mock(side_effect=OSError("database busy")))
        downloader.database = failure
        with self.assertRaises(DownloadFinalizationError) as raised:
            await downloader.download(song, destination)
        self.assertEqual(raised.exception.stage, "database")
        self.assertEqual(raised.exception.path, destination)
        self.assertTrue(destination.is_file())
        downloader.database = MediaDatabase(self.directory / "media.sqlite3")
        downloader._read_track.reset_mock()
        result = await downloader.download(song, destination)
        self.assertEqual(result.status, "skipped")
        downloader._read_track.assert_not_awaited()
        self.assertEqual(downloader.database.get(destination)["media_id"], "111")

    async def test_playlist_failure_is_retryable_after_successful_publication(self):
        downloader, _ = self.downloader()
        video = self.music_video()
        output = self.directory / "video.m4v"
        playlist = self.directory / "playlist.m3u8"
        playlist.write_text("not a valid M3U", encoding="utf-8")
        with self.assertRaises(DownloadFinalizationError) as raised:
            await downloader.download(video, output, playlist_path=playlist)
        self.assertEqual(raised.exception.stage, "playlist")
        self.assertTrue(output.exists())
        self.assertEqual(playlist.read_text(), "not a valid M3U")
        playlist.unlink()
        downloader._read_track.reset_mock()
        retry = await downloader.download(video, output, playlist_path=playlist)
        self.assertEqual(retry.status, "skipped")
        downloader._read_track.assert_not_awaited()
        self.assertEqual(playlist.read_text(), "#EXTM3U\nvideo.m4v\n")

    async def test_get_bytes_does_not_create_database_or_sidecars(self):
        downloader, _ = self.downloader()
        index_path = self.directory / "index.sqlite3"
        downloader.database = MediaDatabase(index_path)
        data = await downloader.get_bytes(self.song())
        self.assertTrue(data.startswith(b"\x00\x00"))
        self.assertFalse(index_path.exists())
        self.assertEqual(list(self.directory.glob("*.jpg")), [])

    def uploaded_video(self, media_id="9876543210"):
        return UploadedVideo.from_metadata(
            {
                "id": media_id,
                "type": "uploaded-videos",
                "attributes": {
                    "name": "Original Upload",
                    "artistName": ".private artist",
                    "uploadDate": "2020-05-01",
                    "assetTokens": {"720pHdVideo": "https://cdn.example.test/file.mp4"},
                },
            },
            api=Mock(),
        )

    async def test_uploaded_video_progressive_download_and_metadata(self):
        video = self.uploaded_video()
        downloader = Downloader()
        downloader._read_track = AsyncMock(
            return_value=(self.video.read_bytes(), "none")
        )
        out = self.directory / "uploaded.m4v"
        result = await downloader.download(
            video,
            out,
            quality_priority=[
                UploadedVideoQuality.HD_1080P,
                UploadedVideoQuality.HD_720P,
            ],
        )
        self.assertEqual(result.status, "downloaded")
        self.assertEqual(result.codec, UploadedVideoQuality.HD_720P)
        self.assertEqual(result.decryption_methods, ("none",))
        self.assertEqual(
            brands(out), brands(self.video)
        )  # progressive bytes are not remuxed
        self.assertEqual(track_hashes(self.video, "v"), track_hashes(out, "v"))
        self.assertEqual(
            mp4_tag_atoms(out)[b"cnID"],
            [(21, (9876543210).to_bytes(8, "big"))],
        )
        args = downloader._read_track.call_args.args
        self.assertEqual(args[0], video.id)
        self.assertEqual(args[1].codec, UploadedVideoQuality.HD_720P)
        self.assertEqual(args[2], "video")

        downloader._read_track.reset_mock()
        second = await downloader.download(video, out)
        self.assertEqual(second.status, "skipped")
        downloader._read_track.assert_not_awaited()

    async def test_uploaded_video_auto_paths_index_and_collision(self):
        db = MediaDatabase(self.directory / "index.sqlite3")
        formatter = PathFormatter(self.directory / "Library")
        downloader = Downloader(path_formatter=formatter, database=db)
        downloader._read_track = AsyncMock(
            return_value=(self.video.read_bytes(), "none")
        )
        video = self.uploaded_video()
        first = await downloader.download(video)
        self.assertEqual(
            first.path,
            self.directory
            / "Library/_private artist/Unknown Album/Original Upload.m4v",
        )
        self.assertEqual(first.status, "downloaded")
        self.assertEqual(
            db.get(first.path)["codec"], UploadedVideoQuality.HD_720P.value
        )
        downloader._read_track.reset_mock()
        self.assertEqual((await downloader.download(video)).status, "skipped")
        downloader._read_track.assert_not_awaited()

        # Identical display names, different IDs: never misattribute a skip.
        second = await downloader.download(self.uploaded_video("5555555555"))
        self.assertEqual(second.status, "downloaded")
        self.assertEqual(second.path.name, "Original Upload 2.m4v")
        self.assertEqual(db.get(second.path)["media_id"], "5555555555")
        self.assertEqual(db.get(first.path)["media_id"], "9876543210")

        # Repeated identity reuses its matching numbered path, not the first
        # free number or another video's filename.
        downloader._read_track.reset_mock()
        repeated = await downloader.download(self.uploaded_video("5555555555"))
        self.assertEqual(repeated.status, "skipped")
        self.assertEqual(repeated.path, second.path)
        downloader._read_track.assert_not_awaited()

    async def test_auto_video_output_extension_and_mp4_brand(self):
        formatter = PathFormatter(self.directory / "Formatted")
        downloader, _ = self.downloader()
        downloader.path_formatter = formatter
        video = self.music_video()
        result = await downloader.download(video, video_extension=".mp4")
        self.assertEqual(result.path.suffix, ".mp4")
        self.assertEqual(brands(result.path)[0], b"mp42")

    async def test_auto_paths_without_index_recognize_embedded_cnid(self):
        formatter = PathFormatter(self.directory / "Formatted")
        downloader = Downloader(path_formatter=formatter)
        downloader._read_track = AsyncMock(
            return_value=(self.video.read_bytes(), "none")
        )
        video = self.uploaded_video()
        first = await downloader.download(video)
        self.assertEqual(first.status, "downloaded")
        # As in GAMDL 3.x, re-read the native cnID tag when no index exists.
        downloader._read_track.reset_mock()
        second = await downloader.download(video)
        self.assertEqual(second.status, "skipped")
        self.assertEqual(second.path, first.path)
        downloader._read_track.assert_not_awaited()

        other = await downloader.download(self.uploaded_video("5555555555"))
        self.assertEqual(other.status, "downloaded")
        self.assertEqual(other.path.name, "Original Upload 2.m4v")
        downloader._read_track.reset_mock()
        repeated = await downloader.download(self.uploaded_video("5555555555"))
        self.assertEqual(repeated.status, "skipped")
        self.assertEqual(repeated.path, other.path)
        downloader._read_track.assert_not_awaited()

    async def test_auto_video_collision_uses_next_number_and_reuses_gaps(self):
        formatter = PathFormatter(self.directory / "Numbered")
        downloader = Downloader(path_formatter=formatter)
        downloader._read_track = AsyncMock(
            return_value=(self.video.read_bytes(), "none")
        )
        first = await downloader.download(self.uploaded_video("100"))
        second = await downloader.download(self.uploaded_video("200"))
        third = await downloader.download(self.uploaded_video("300"))
        self.assertEqual(
            [p.path.name for p in (first, second, third)],
            ["Original Upload.m4v", "Original Upload 2.m4v", "Original Upload 3.m4v"],
        )
        second.path.unlink()
        reuse = await downloader.download(self.uploaded_video("400"))
        self.assertEqual(reuse.path, second.path)
        self.assertEqual(reuse.status, "downloaded")
        self.assertEqual(
            (await downloader.download(self.uploaded_video("300"))).path, third.path
        )

    async def test_concurrent_collision_does_not_skip_other_video(self):
        formatter = PathFormatter(self.directory / "Numbered")
        database = MediaDatabase(self.directory / "index.sqlite3")
        downloader = Downloader(path_formatter=formatter, database=database)
        downloader._read_track = AsyncMock(
            return_value=(self.video.read_bytes(), "none")
        )
        actual_publish = __import__(
            "gamdl.download.publication", fromlist=["publish_bytes"]
        ).publish_bytes
        raced = False

        def racing_publish(data, destination, *, overwrite):
            nonlocal raced
            if not raced:
                raced = True
                # A competing process got the original name while we were
                # downloading. Our first atomic link attempt now fails.
                actual_publish(b"other video", destination, overwrite="skip")
                return False
            return actual_publish(data, destination, overwrite=overwrite)

        with patch(
            "gamdl.download.downloader.publish_bytes", side_effect=racing_publish
        ):
            result = await downloader.download(self.uploaded_video("555"))
        self.assertTrue(raced)
        self.assertEqual(result.status, "downloaded")
        self.assertEqual(result.path.name, "Original Upload 2.m4v")
        self.assertEqual(
            result.path.with_name("Original Upload.m4v").read_bytes(), b"other video"
        )
        self.assertEqual(database.get(result.path)["media_id"], "555")

    async def test_auto_replace_cannot_clobber_newly_claimed_video_slot(self):
        formatter = PathFormatter(self.directory / "Numbered")
        downloader = Downloader(path_formatter=formatter)
        downloader._read_track = AsyncMock(
            return_value=(self.video.read_bytes(), "none")
        )
        from gamdl.download.publication import publish_bytes as real_publish

        raced = False

        def competing_publish(data, destination, *, overwrite):
            nonlocal raced
            if not raced:
                raced = True
                real_publish(b"other video", destination, overwrite="skip")
                self.assertEqual(overwrite, "skip")
                return False
            return real_publish(data, destination, overwrite=overwrite)

        with patch(
            "gamdl.download.downloader.publish_bytes", side_effect=competing_publish
        ):
            result = await downloader.download(
                self.uploaded_video("555"), overwrite="replace"
            )
        self.assertEqual(result.status, "downloaded")
        self.assertEqual(result.path.name, "Original Upload 2.m4v")
        self.assertEqual(
            result.path.with_name("Original Upload.m4v").read_bytes(), b"other video"
        )

    async def test_formatter_requires_configuration_if_output_missing(self):
        with self.assertRaises(ValueError):
            await Downloader().download(self.song())

    async def test_uploaded_video_codec_and_quality_validation(self):
        video = self.uploaded_video()
        downloader = Downloader()
        with self.assertRaises(ValueError):
            await downloader.get_bytes(video, codec=SongCodec.AAC)
        with self.assertRaises(ValueError):
            await downloader.get_bytes(self.song(), quality=UploadedVideoQuality.SD)
