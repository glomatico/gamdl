"""Offline integration tests for the reusable one-item download workflow."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from gamdl.download import Downloader, DownloadProgress, Tags
from gamdl.media import MusicVideo, MusicVideoCodec, Song, SongCodec
from gamdl.media.music_video.stream import MusicVideoStreams
from gamdl.stream import StreamProgress
from tests.test_muxer import brands, mp4_tag_atoms, track_hashes


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
        with patch("gamdl.download.downloader.os.link", side_effect=FileExistsError):
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
