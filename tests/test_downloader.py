"""Offline integration tests for the reusable one-item download workflow."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch

from gamdl.download import (
    Downloader,
    DownloadFinalizationError,
    DownloadProgress,
    DownloadResult,
    MediaDatabase,
    MediaStatus,
    PathFormatter,
    Tags,
)
from gamdl.download.playlist import PlaylistWriter
from gamdl.download.publication import (
    publish_bytes_result as original_publish_sidecar,
)
from gamdl.download.publication import (
    publish_file as original_publish_file,
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
from gamdl.media.music_video.stream import MusicVideoStreams
from gamdl.media.song.lyrics import Lyrics, LyricsLine
from gamdl.stream import StreamProgress
from tests.test_muxer import JPEG, PNG, brands, mp4_tag_atoms, track_hashes


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and FFprobe required for downloader integration fixtures",
)
class DownloaderTests(unittest.IsolatedAsyncioTestCase):
    def test_file_publication_cross_filesystem_fallback(self):
        source = self.directory / "staged.bin"
        source.write_bytes(b"example-media" * 8000)
        destination = self.directory / "nested" / "published.bin"
        # A source on a separate filesystem needs no special-case logic:
        # publication always copies into the destination directory first.
        with patch(
            "gamdl.download.publication.os.link",
            side_effect=AssertionError("Hardlinks are forbidden"),
        ):
            self.assertTrue(
                original_publish_file(source, destination, overwrite="skip")
            )
        self.assertEqual(destination.read_bytes(), source.read_bytes())
        self.assertTrue(source.exists())
        self.assertEqual(list(destination.parent.glob(".*.part")), [])

    def test_file_publication_replaces_existing_file_and_rejects_race(self):
        source = self.directory / "staged.bin"
        source.write_bytes(b"new content")
        target = self.directory / "published.bin"
        target.write_bytes(b"old")
        self.assertFalse(original_publish_file(source, target, overwrite="skip"))
        with self.assertRaises(ValueError):
            original_publish_file(source, target, overwrite="error")
        self.assertTrue(original_publish_file(source, target, overwrite="replace"))
        self.assertEqual(target.read_bytes(), b"new content")
        self.assertTrue(source.exists())
        self.assertEqual(list(self.directory.glob(".*.part")), [])
        self.assertFalse(list(self.directory.glob(".published.bin.*")))

    def test_file_publication_replace_uses_independent_copy(self):
        source = self.directory / "staged.bin"
        source.write_bytes(b"new content")
        target = self.directory / "published.bin"
        target.write_bytes(b"previous media")
        with patch(
            "gamdl.download.publication.os.link",
            side_effect=AssertionError("Hardlinks are forbidden"),
        ):
            self.assertTrue(original_publish_file(source, target, overwrite="replace"))
        self.assertEqual(target.read_bytes(), b"new content")
        self.assertNotEqual(target.stat().st_ino, source.stat().st_ino)
        self.assertFalse(list(self.directory.glob(".published.bin.*")))

    def test_file_publication_replace_copies_independently(self):
        source = self.directory / "staged.bin"
        source.write_bytes(b"example-media" * 8000)
        target = self.directory / "published.bin"
        target.write_bytes(b"old")
        self.assertTrue(original_publish_file(source, target, overwrite="replace"))
        self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertNotEqual(target.stat().st_ino, source.stat().st_ino)
        self.assertFalse(list(self.directory.glob(".published.bin.*")))

    def test_downloader_accepts_explicit_temp_root(self):
        root = self.directory / "scratch"
        self.assertEqual(Downloader(temp_path=root).temp_path, root)
        with self.assertRaises(ValueError):
            Downloader(temp_path="")

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
        media.get_lyrics = AsyncMock(return_value=None)
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

        async def save(
            _media_id, _source, track, destination, _callback, *, event_callback=None
        ):
            shutil.copyfile(self.video if track == "video" else self.audio, destination)
            return "none"

        downloader._save_track = AsyncMock(side_effect=save)
        return downloader, tag_resolver

    async def test_song_bytes_remux_tags_and_default_lyrics(self):
        downloader, tags = self.downloader()
        song = self.song()
        song.get_lyrics = AsyncMock(
            return_value=Lyrics(stanzas=[[LyricsLine("These are sample lyrics")]])
        )
        value = await downloader.get_bytes(
            song,
            codec_priority=[SongCodec.ALAC_HIRES, SongCodec.AAC],
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
            [SongCodec.ALAC_HIRES, SongCodec.AAC],
        )
        tags.get_tags.assert_awaited_once_with(song, codec=SongCodec.AAC)

    async def test_song_bytes_excluded_lyrics_are_not_fetched(self):
        downloader, _ = self.downloader()
        song = self.song()
        song.get_lyrics = AsyncMock(
            side_effect=AssertionError("Excluded lyrics must not be fetched")
        )
        result = await downloader.get_bytes(song, exclude_tags=("lyrics",))
        output = self.directory / "no-embedded-lyrics.m4a"
        output.write_bytes(result)
        self.assertNotIn(b"\xa9lyr", mp4_tag_atoms(output))
        song.get_lyrics.assert_not_awaited()

    async def test_web_codec_tags_use_actual_selected_codec(self):
        downloader, resolver = self.downloader()
        song = self.song()
        song.get_stream.return_value.codec = SongCodec.AAC_WEB
        await downloader.get_bytes(song, codec=SongCodec.AAC_WEB)
        resolver.get_tags.assert_awaited_once_with(song, codec=SongCodec.AAC_WEB)

    async def test_auto_path_for_web_codec_uses_web_tags_before_stream_selection(self):
        downloader, resolver = self.downloader()
        downloader.path_formatter = PathFormatter(self.directory / "Music")
        song = self.song()
        song.get_stream.return_value.codec = SongCodec.AAC_WEB
        await downloader.download(song, codec=SongCodec.AAC_WEB)
        resolver.get_tags.assert_awaited_once_with(song, codec=SongCodec.AAC_WEB)
        song.get_stream.assert_awaited_once()

    async def test_auto_path_mixed_codec_priorities_preselects_once(self):
        downloader, resolver = self.downloader()
        downloader.path_formatter = PathFormatter(self.directory / "Music")
        song = self.song()
        song.get_stream.return_value.codec = SongCodec.AAC_WEB
        outcome = await downloader.download(
            song, codec_priority=(SongCodec.ALAC_HIRES, SongCodec.AAC_WEB)
        )
        self.assertTrue(outcome.path.exists())
        self.assertEqual(outcome.codec, SongCodec.AAC_WEB)
        resolver.get_tags.assert_awaited_once_with(song, codec=SongCodec.AAC_WEB)
        song.get_stream.assert_awaited_once()

    async def test_auto_path_interactive_selection_uses_selected_codec(self):
        downloader, resolver = self.downloader()
        downloader.path_formatter = PathFormatter(self.directory / "Music")
        song = self.song()
        song.get_stream.return_value.codec = SongCodec.AAC_HE_WEB
        selector = Mock(return_value=0)
        await downloader.download(song, ask=True, selector=selector)
        resolver.get_tags.assert_awaited_once_with(song, codec=SongCodec.AAC_HE_WEB)
        song.get_stream.assert_awaited_once()

    async def test_mux_and_tagger_run_off_event_loop(self):
        downloader, _ = self.downloader()
        song = self.song()
        main_thread = threading.get_ident()
        entered = threading.Event()
        release = threading.Event()
        threads = {}
        original_remux = downloader.muxer.remux
        original_tag = downloader.muxer.tag

        def remux(data):
            threads["remux"] = threading.get_ident()
            entered.set()
            if not release.wait(3):
                raise TimeoutError("The event loop never released the muxer")
            return original_remux(data)

        def tag(path, *, tags, cover_bytes=None):
            threads["tag"] = threading.get_ident()
            return original_tag(path, tags=tags, cover_bytes=cover_bytes)

        with (
            patch.object(downloader.muxer, "remux", side_effect=remux),
            patch.object(downloader.muxer, "tag", side_effect=tag),
        ):
            task = asyncio.create_task(downloader.get_bytes(song))
            try:
                # This would time out if the blocking muxer ran on the loop.
                ready = await asyncio.wait_for(asyncio.to_thread(entered.wait), 2)
                self.assertTrue(ready)
            finally:
                release.set()
            self.assertTrue((await task).startswith(b"\x00\x00"))
        self.assertNotEqual(threads["remux"], main_thread)
        self.assertNotEqual(threads["tag"], main_thread)

    async def test_atomic_publication_completes_before_cancellation_propagates(self):
        downloader, _ = self.downloader()
        song = self.song()
        output = self.directory / "atomic-cancel.m4a"
        entered = threading.Event()
        release = threading.Event()

        def slow_publish(source, destination, *, overwrite):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("Timed out awaiting completion of publication")
            return original_publish_file(source, destination, overwrite=overwrite)

        with patch("gamdl.download.downloader.publish_file", side_effect=slow_publish):
            task = asyncio.create_task(downloader.download(song, output, tag=False))
            try:
                ready = await asyncio.wait_for(asyncio.to_thread(entered.wait), 2)
                self.assertTrue(ready)
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(output.is_file())
        self.assertEqual(list(self.directory.glob(".atomic-cancel*")), [])

    async def test_sidecar_database_and_playlist_writes_run_off_event_loop(self):
        downloader, _ = self.downloader()
        db = MediaDatabase(self.directory / "offloaded.sqlite3")
        downloader.database = db
        song = self.song()
        song.source = MediaSource("playlist_tracks", "id", 0)
        song.get_cover.return_value = Mock(get_cover_bytes=AsyncMock(return_value=JPEG))
        song.get_lyrics = AsyncMock(return_value=Lyrics(stanzas=[[LyricsLine("Test")]]))
        output = self.directory / "offloaded.m4a"
        playlist = self.directory / "offloaded.m3u8"
        main = threading.get_ident()
        workers: dict[str, list[int]] = {}

        def track(label, function):
            def run(*args, **kwargs):
                workers.setdefault(label, []).append(threading.get_ident())
                return function(*args, **kwargs)

            return run

        with (
            patch(
                "gamdl.download.downloader.publish_file",
                side_effect=track("media", original_publish_file),
            ),
            patch(
                "gamdl.download.downloader.publish_bytes_result",
                side_effect=track("sidecars", original_publish_sidecar),
            ),
            patch.object(db, "record", side_effect=track("database", db.record)),
            patch.object(
                PlaylistWriter,
                "add",
                new=track("playlist", PlaylistWriter.add),
            ),
        ):
            result = await downloader.download(
                song,
                output,
                tag=False,
                save_cover=True,
                save_lyrics=True,
                lyrics_file_format="text",
                playlist_path=playlist,
            )

        self.assertEqual(result.status.value, "downloaded")
        self.assertEqual(
            {name: len(ids) for name, ids in workers.items()},
            {"media": 1, "sidecars": 2, "database": 1, "playlist": 1},
        )
        self.assertTrue(all(tid != main for ids in workers.values() for tid in ids))
        self.assertTrue(output.exists())
        self.assertEqual(output.with_suffix(".jpg").read_bytes(), JPEG)
        self.assertEqual(output.with_suffix(".txt").read_text(), "Test")
        self.assertTrue(playlist.exists())
        self.assertIsNotNone(db.get(output))

    async def test_atomic_download_skip_and_replace(self):
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
        with self.assertRaises(TypeError):
            await downloader.download(song, output, overwrite="error")
        result = await downloader.download(song, output, overwrite=True)
        self.assertEqual(result.status, "overwritten")
        self.assertEqual(result.codec, SongCodec.AAC)
        self.assertEqual(result.decryption_methods, ("none",))
        self.assertEqual(result.bytes_written, output.stat().st_size)
        self.assertEqual(brands(output)[0], b"M4A ")
        self.assertEqual(list(output.parent.glob(".*.part")), [])

    async def test_database_early_skip_avoids_all_media_and_existing_sidecar_requests(
        self,
    ):
        downloader, tag_resolver = self.downloader()
        output_root = self.directory / "library"
        media_path = output_root / "Sample Artist/Unknown Album/Sample Track.m4a"
        media_path.parent.mkdir(parents=True)
        media_path.write_bytes(self.audio.read_bytes())
        media_path.with_suffix(".lrc").write_text("[00:01.00]Existing lyrics")
        media_path.with_suffix(".jpg").write_bytes(JPEG)
        db = MediaDatabase(self.directory / "fast.sqlite3")
        db.index_file(media_path, "songs", "111")
        downloader.database = db
        downloader.path_formatter = PathFormatter(output_root)
        song = self.song()
        song.is_uploaded = AsyncMock(
            side_effect=AssertionError("Unnecessary API lookup")
        )
        song.get_lyrics = AsyncMock(
            side_effect=AssertionError("Unnecessary lyrics API")
        )
        song.get_cover = AsyncMock(side_effect=AssertionError("Unnecessary cover API"))
        playlist = output_root / "Playlists/example.m3u8"
        result = await downloader.download(
            song,
            codec_priority=(SongCodec.ATMOS, SongCodec.AAC),
            save_cover=True,
            save_lyrics=True,
            playlist_path=playlist,
            playlist_index=0,
        )
        self.assertEqual(result.status.value, "skipped")
        self.assertEqual(result.path, media_path)
        self.assertEqual(result.lyrics.status.value, "skipped")
        self.assertEqual(result.cover.status.value, "skipped")
        self.assertTrue(playlist.is_file())
        self.assertIn("Sample Track.m4a", playlist.read_text())
        tag_resolver.get_tags.assert_not_awaited()
        downloader._save_track.assert_not_awaited()
        song.is_uploaded.assert_not_awaited()
        song.get_lyrics.assert_not_awaited()
        song.get_cover.assert_not_awaited()

    async def test_single_codec_priority_does_not_reuse_unverified_database_codec(self):
        downloader, tag_resolver = self.downloader()
        output_root = self.directory / "library"
        media_path = output_root / "Sample Artist/Unknown Album/Sample Track.m4a"
        media_path.parent.mkdir(parents=True)
        media_path.write_bytes(self.audio.read_bytes())
        db = MediaDatabase(self.directory / "fast.sqlite3")
        db.index_file(media_path, "songs", "111")
        downloader.database = db
        downloader.path_formatter = PathFormatter(output_root)
        song = self.song()
        result = await downloader.download(song, codec_priority=(SongCodec.ALAC_HIRES,))
        self.assertEqual(result.status.value, "skipped")
        tag_resolver.get_tags.assert_awaited_once()

    async def test_generic_alac_single_priority_reuses_both_concrete_tiers(self):
        for codec in (SongCodec.ALAC_HIRES, SongCodec.ALAC_LOSSLESS):
            with self.subTest(codec=codec):
                downloader, tag_resolver = self.downloader()
                output_root = self.directory / f"library-{codec.value}"
                media_path = (
                    output_root / "Sample Artist/Unknown Album/Sample Track.m4a"
                )
                media_path.parent.mkdir(parents=True)
                media_path.write_bytes(self.audio.read_bytes())
                db = MediaDatabase(
                    self.directory / f"{codec.value}.sqlite3", root=output_root
                )
                song = self.song()
                db.record(
                    song,
                    DownloadResult(
                        song.id,
                        media_path,
                        MediaStatus.DOWNLOADED,
                        media_path.stat().st_size,
                        codec,
                    ),
                )
                downloader.database = db
                downloader.path_formatter = PathFormatter(output_root)
                song.is_uploaded = AsyncMock(
                    side_effect=AssertionError("Index hit must avoid API requests")
                )
                result = await downloader.download(
                    song, codec_priority=(SongCodec.ALAC,)
                )
                self.assertEqual(result.status, MediaStatus.SKIPPED)
                self.assertEqual(result.path, media_path)
                tag_resolver.get_tags.assert_not_awaited()
                song.is_uploaded.assert_not_awaited()

    async def test_generic_alac_does_not_reuse_existing_aac(self):
        downloader, tag_resolver = self.downloader()
        output_root = self.directory / "library-aac"
        media_path = output_root / "Sample Artist/Unknown Album/Sample Track.m4a"
        media_path.parent.mkdir(parents=True)
        media_path.write_bytes(self.audio.read_bytes())
        db = MediaDatabase(self.directory / "aac.sqlite3", root=output_root)
        song = self.song()
        db.record(
            song,
            DownloadResult(
                song.id,
                media_path,
                MediaStatus.DOWNLOADED,
                media_path.stat().st_size,
                SongCodec.AAC,
            ),
        )
        downloader.database = db
        downloader.path_formatter = PathFormatter(output_root)
        result = await downloader.download(song, codec_priority=(SongCodec.ALAC,))
        self.assertEqual(result.status, MediaStatus.SKIPPED)
        # The filename collision still prevents replacing the file, but the
        # AAC index entry must not be mistaken for an ALAC early cache hit.
        tag_resolver.get_tags.assert_awaited_once()

    async def test_explicit_codec_and_replace_bypass_database_early_skip(self):
        downloader, tag_resolver = self.downloader()
        root = self.directory / "library"
        destination = root / "Sample Artist/Unknown Album/Sample Track.m4a"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(self.audio.read_bytes())
        db = MediaDatabase(self.directory / "fast.sqlite3")
        db.index_file(destination, "songs", "111")
        downloader.database = db
        downloader.path_formatter = PathFormatter(root)
        song = self.song()
        await downloader.download(song, codec=SongCodec.ALAC_HIRES)
        tag_resolver.get_tags.assert_awaited_once()
        tag_resolver.get_tags.reset_mock()
        result = await downloader.download(song, overwrite=True, codec=SongCodec.AAC)
        self.assertEqual(result.status.value, "overwritten")
        tag_resolver.get_tags.assert_awaited()

    async def test_custom_temp_path_stages_on_disk_and_cleans_on_success(self):
        root = self.directory / "custom-staging"
        downloader, _ = self.downloader()
        downloader.temp_path = root
        song = self.song()
        output = self.directory / "destination" / "song.m4a"
        seen = []
        original = downloader.muxer.remux_file

        def remux(source, destination):
            seen.append((Path(source), Path(destination)))
            self.assertEqual(Path(source).parent.parent, root)
            self.assertGreater(Path(source).stat().st_size, 0)
            return original(source, destination)

        with (
            patch.object(downloader.muxer, "remux_file", side_effect=remux),
            patch.object(
                downloader, "_read_track", side_effect=AssertionError("Not disk-backed")
            ),
        ):
            result = await downloader.download(song, output)
        self.assertEqual(result.status, "downloaded")
        self.assertEqual(len(seen), 1)
        self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(list(output.parent.glob("*.part")), [])
        self.assertTrue(output.is_file())

    async def test_custom_temp_path_is_cleaned_after_mux_failure(self):
        downloader, _ = self.downloader()
        root = self.directory / "failed-staging"
        downloader.temp_path = root
        output = self.directory / "broken.m4a"
        with patch.object(
            downloader.muxer, "remux_file", side_effect=ValueError("bad MP4")
        ):
            with self.assertRaisesRegex(ValueError, "bad MP4"):
                await downloader.download(self.song(), output)
        self.assertFalse(output.exists())
        self.assertEqual(list(root.iterdir()), [])

    async def test_get_bytes_does_not_use_disk_backed_path(self):
        downloader, _ = self.downloader()
        with patch.object(
            downloader, "_save_track", side_effect=AssertionError("Not in-memory")
        ):
            data = await downloader.get_bytes(self.song(), tag=False)
        self.assertTrue(data.startswith(b"\x00\x00"))

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

        async def fail_one(
            _media_id, _source, track, _destination, _callback, *, event_callback=None
        ):
            if track == "video":
                await audio_started.wait()
                raise RuntimeError("video failed")
            audio_started.set()
            try:
                await asyncio.sleep(3600)
            finally:
                cancelled.set()

        downloader._save_track = AsyncMock(side_effect=fail_one)
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
        result = await downloader.download(song, output, overwrite=False)
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

        async def slow_track(
            _id, _source, track, _destination, _callback, *, event_callback=None
        ):
            starts.add(track)
            if len(starts) == 2:
                both_started.set()
            try:
                await asyncio.sleep(3600)
            finally:
                both_cancelled.add(track)

        downloader._save_track = AsyncMock(side_effect=slow_track)
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
        with self.assertRaises(TypeError):
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
        db = MediaDatabase(
            self.directory / "db" / "media.sqlite3",
            root=self.directory,
            save_metadata=True,
        )
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
            exclude_tags=("artist",),
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
        self.assertEqual(db.get_metadata(output, "songs")["artist"], "Sample Artist")
        self.assertNotIn(b"\xa9ART", mp4_tag_atoms(output))
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

    async def test_png_cover_format_used_for_tags_and_sidecar_retry(self):
        downloader, _ = self.downloader()
        song = self.song()
        cover = Mock(get_cover_bytes=AsyncMock(return_value=PNG))
        song.get_cover.return_value = cover
        output = self.directory / "png-cover.m4a"
        result = await downloader.download(
            song, output, cover_format="png", save_cover=True
        )
        self.assertEqual(result.status, "downloaded")
        cover.get_cover_bytes.assert_awaited_once_with(
            width=1200, height=1200, format="png"
        )
        self.assertEqual(output.with_suffix(".png").read_bytes(), PNG)
        self.assertFalse(output.with_suffix(".jpg").exists())
        self.assertIn(b"covr", mp4_tag_atoms(output))

        output.with_suffix(".png").unlink()
        cover.get_cover_bytes.reset_mock()
        song.get_stream.reset_mock()
        result = await downloader.download(
            song, output, cover_format="png", save_cover=True
        )
        self.assertEqual(result.status, "skipped")
        cover.get_cover_bytes.assert_awaited_once_with(
            width=1200, height=1200, format="png"
        )
        song.get_stream.assert_not_awaited()
        self.assertEqual(output.with_suffix(".png").read_bytes(), PNG)

    async def test_embedded_and_file_cover_sizes_are_independent(self):
        downloader, _ = self.downloader()
        song = self.song()
        cover = Mock(get_cover_bytes=AsyncMock(return_value=PNG))
        song.get_cover.return_value = cover
        destination = self.directory / "different-cover-sizes.m4a"

        result = await downloader.download(
            song,
            destination,
            cover_format="png",
            embedded_cover_size=300,
            file_cover_size=2400,
            save_cover=True,
        )
        self.assertEqual(result.status, "downloaded")
        self.assertEqual(destination.with_suffix(".png").read_bytes(), PNG)
        self.assertIn(b"covr", mp4_tag_atoms(destination))
        cover.get_cover_bytes.assert_has_awaits(
            [
                call(width=300, height=300, format="png"),
                call(width=2400, height=2400, format="png"),
            ]
        )
        self.assertEqual(cover.get_cover_bytes.await_count, 2)

        destination.with_suffix(".png").unlink()
        cover.get_cover_bytes.reset_mock()
        result = await downloader.download(
            song,
            destination,
            cover_format="png",
            embedded_cover_size=300,
            file_cover_size=2400,
            save_cover=True,
        )
        self.assertEqual(result.status, "skipped")
        cover.get_cover_bytes.assert_awaited_once_with(
            width=2400, height=2400, format="png"
        )

    async def test_matching_cover_sizes_reuse_artwork(self):
        downloader, _ = self.downloader()
        song = self.song()
        cover = Mock(get_cover_bytes=AsyncMock(return_value=JPEG))
        song.get_cover.return_value = cover
        destination = self.directory / "matching-cover-sizes.m4a"
        await downloader.download(
            song,
            destination,
            embedded_cover_size=600,
            file_cover_size=600,
            save_cover=True,
        )
        cover.get_cover_bytes.assert_awaited_once_with(
            width=600, height=600, format="jpg"
        )

    async def test_maximum_cover_sizes_support_embedded_and_sidecar(self):
        downloader, _ = self.downloader()
        song = self.song()
        cover = Mock(get_cover_bytes=AsyncMock(return_value=PNG))
        song.get_cover.return_value = cover
        destination = self.directory / "max-artwork.m4a"

        await downloader.download(
            song,
            destination,
            cover_format="png",
            embedded_cover_size=600,
            file_cover_size="max",
            save_cover=True,
        )
        self.assertIn(b"covr", mp4_tag_atoms(destination))
        self.assertEqual(destination.with_suffix(".png").read_bytes(), PNG)
        cover.get_cover_bytes.assert_has_awaits(
            [
                call(width=600, height=600, format="png"),
                call(width="max", height="max", format="png"),
            ]
        )
        self.assertEqual(cover.get_cover_bytes.await_count, 2)

        destination.with_suffix(".png").unlink()
        cover.get_cover_bytes.reset_mock()
        result = await downloader.download(
            song,
            destination,
            cover_format="png",
            embedded_cover_size=600,
            file_cover_size="max",
            save_cover=True,
        )
        self.assertEqual(result.status, "skipped")
        cover.get_cover_bytes.assert_awaited_once_with(
            width="max", height="max", format="png"
        )

        song.get_cover.reset_mock()
        cover.get_cover_bytes.reset_mock()
        data = await downloader.get_bytes(
            song, cover_format="png", embedded_cover_size="max"
        )
        self.assertTrue(data)
        cover.get_cover_bytes.assert_awaited_once_with(
            width="max", height="max", format="png"
        )

    async def test_equal_maximum_cover_sizes_share_one_fetch(self):
        downloader, _ = self.downloader()
        song = self.song()
        cover = Mock(get_cover_bytes=AsyncMock(return_value=JPEG))
        song.get_cover.return_value = cover
        destination = self.directory / "both-max.m4a"
        await downloader.download(
            song,
            destination,
            embedded_cover_size="max",
            file_cover_size="max",
            save_cover=True,
        )
        cover.get_cover_bytes.assert_awaited_once_with(
            width="max", height="max", format="jpg"
        )

    async def test_untagged_download_only_fetches_file_cover_size(self):
        downloader, _ = self.downloader()
        song = self.song()
        cover = Mock(get_cover_bytes=AsyncMock(return_value=JPEG))
        song.get_cover.return_value = cover
        destination = self.directory / "file-only-cover.m4a"
        await downloader.download(
            song,
            destination,
            tag=False,
            embedded_cover_size=400,
            file_cover_size=1800,
            save_cover=True,
        )
        cover.get_cover_bytes.assert_awaited_once_with(
            width=1800, height=1800, format="jpg"
        )

    async def test_invalid_cover_sizes_rejected_by_python_api(self):
        downloader, _ = self.downloader()
        song = self.song()
        destination = self.directory / "invalid-cover-size.m4a"
        for size in (0, -1, 2.5, True, "1200", "MAX", None):
            with self.subTest(size=size):
                with self.assertRaisesRegex(ValueError, "embedded_cover_size"):
                    await downloader.get_bytes(song, embedded_cover_size=size)
                with self.assertRaisesRegex(ValueError, "file_cover_size"):
                    await downloader.download(song, destination, file_cover_size=size)

    async def test_date_tag_template_only_changes_written_tag_not_saved_metadata(self):
        downloader, resolver = self.downloader()
        db = MediaDatabase(
            self.directory / "date-index.sqlite3",
            root=self.directory,
            save_metadata=True,
        )
        downloader.database = db
        original_date = "2004-11-12T12:30:00Z"
        tags = Tags(
            title="Date Example",
            artist="Example",
            title_id=111,
            release_date=original_date,
        )
        resolver.get_tags.return_value = tags
        song = self.song()
        output = self.directory / "date-template.m4a"

        result = await downloader.download(song, output, date_tag_template="%Y-%m")
        self.assertEqual(result.status, "downloaded")
        self.assertEqual(mp4_tag_atoms(output)[b"\xa9day"], [(1, b"2004-11")])
        self.assertEqual(
            db.get_metadata(output, "songs")["release_date"], original_date
        )
        self.assertEqual(resolver.get_tags.return_value.release_date, original_date)

        bytes_result = await downloader.get_bytes(song, date_tag_template="%Y")
        bytes_file = self.directory / "date-template-bytes.m4a"
        bytes_file.write_bytes(bytes_result)
        self.assertEqual(mp4_tag_atoms(bytes_file)[b"\xa9day"], [(1, b"2004")])

        default = self.directory / "date-default.m4a"
        await downloader.download(song, default)
        self.assertEqual(
            mp4_tag_atoms(default)[b"\xa9day"], [(1, b"2004-11-12T12:30:00Z")]
        )

        no_date_tag = self.directory / "date-excluded.m4a"
        await downloader.download(
            song, no_date_tag, date_tag_template="%Y", exclude_tags=("release_date",)
        )
        self.assertNotIn(b"\xa9day", mp4_tag_atoms(no_date_tag))

    async def test_invalid_date_tag_templates_rejected_without_download(self):
        downloader, _ = self.downloader()
        song = self.song()
        target = self.directory / "bad-date-template.m4a"
        for invalid in ("", "  ", None, 123):
            with self.subTest(value=invalid):
                with self.assertRaisesRegex(ValueError, "date_tag_template"):
                    await downloader.download(song, target, date_tag_template=invalid)
                with self.assertRaisesRegex(ValueError, "date_tag_template"):
                    await downloader.get_bytes(song, date_tag_template=invalid)
        self.assertFalse(target.exists())

    async def test_get_bytes_cover_format_and_invalid_raw(self):
        downloader, _ = self.downloader()
        song = self.song()
        cover = Mock(get_cover_bytes=AsyncMock(return_value=PNG))
        song.get_cover.return_value = cover
        data = await downloader.get_bytes(song, cover_format="png")
        cover.get_cover_bytes.assert_awaited_once_with(
            width=1200, height=1200, format="png"
        )
        output = self.directory / "returned.m4a"
        output.write_bytes(data)
        self.assertIn(b"covr", mp4_tag_atoms(output))
        with self.assertRaises(ValueError):
            await downloader.get_bytes(song, cover_format="raw", tag=False)
        with self.assertRaises(ValueError):
            await downloader.download(song, output, cover_format="raw", tag=False)

    async def test_unsynced_song_lyrics_use_txt_and_png_cover(self):
        downloader, _ = self.downloader()
        song = self.song()
        song.get_lyrics = AsyncMock(
            return_value=Lyrics(stanzas=[[LyricsLine("Hello")]])
        )
        song.get_cover.return_value = Mock(get_cover_bytes=AsyncMock(return_value=PNG))
        output = self.directory / "unsynced.m4a"
        await downloader.download(
            song,
            output,
            tag=False,
            save_cover=True,
            save_lyrics=True,
            lyrics_file_format="text",
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
        with self.assertWarnsRegex(UserWarning, "database registration failed"):
            completed = await downloader.download(song, destination)
        self.assertEqual(completed.status, "downloaded")
        self.assertIsNone(completed.database)
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
        self.assertEqual(raised.exception.partial_result.path, output)
        self.assertIsNone(raised.exception.partial_result.playlist_file)
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

    def mock_uploaded_download(self, downloader):
        async def save(
            _media_id, _source, _track, destination, _callback, *, event_callback=None
        ):
            shutil.copyfile(self.video, destination)
            return "none"

        downloader._save_track = AsyncMock(side_effect=save)

    async def test_uploaded_video_progressive_download_and_metadata(self):
        video = self.uploaded_video()
        downloader = Downloader()
        self.mock_uploaded_download(downloader)
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
        args = downloader._save_track.call_args.args
        self.assertEqual(args[0], video.id)
        self.assertEqual(args[1].codec, UploadedVideoQuality.HD_720P)
        self.assertEqual(args[2], "video")

        downloader._save_track.reset_mock()
        second = await downloader.download(video, out)
        self.assertEqual(second.status, "skipped")
        downloader._save_track.assert_not_awaited()

    async def test_uploaded_video_auto_paths_index_and_collision(self):
        db = MediaDatabase(self.directory / "index.sqlite3")
        formatter = PathFormatter(self.directory / "Library")
        downloader = Downloader(path_formatter=formatter, database=db)
        self.mock_uploaded_download(downloader)
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
        downloader._save_track.reset_mock()
        self.assertEqual((await downloader.download(video)).status, "skipped")
        downloader._save_track.assert_not_awaited()

        # Identical display names, different IDs: never misattribute a skip.
        second = await downloader.download(self.uploaded_video("5555555555"))
        self.assertEqual(second.status, "downloaded")
        self.assertEqual(second.path.name, "Original Upload 2.m4v")
        self.assertEqual(db.get(second.path)["media_id"], "5555555555")
        self.assertEqual(db.get(first.path)["media_id"], "9876543210")

        # Repeated identity reuses its matching numbered path, not the first
        # free number or another video's filename.
        downloader._save_track.reset_mock()
        repeated = await downloader.download(self.uploaded_video("5555555555"))
        self.assertEqual(repeated.status, "skipped")
        self.assertEqual(repeated.path, second.path)
        downloader._save_track.assert_not_awaited()

    async def test_auto_video_output_extension_and_mp4_brand(self):
        formatter = PathFormatter(self.directory / "Formatted")
        downloader, _ = self.downloader()
        downloader.path_formatter = formatter
        video = self.music_video()
        result = await downloader.download(video, video_extension=".mp4")
        self.assertEqual(result.path.suffix, ".mp4")
        self.assertEqual(brands(result.path)[0], b"mp42")

    async def test_auto_song_without_database_skips_matching_cnid(self):
        """Regression: `dump_stream --auto-path` created an unwanted [cnID] copy."""
        downloader, _ = self.downloader()
        downloader.path_formatter = PathFormatter(self.directory / "Library")
        song = self.song()
        first = await downloader.download(song)
        self.assertEqual(first.status, "downloaded")
        self.assertEqual(first.path.name, "Sample Track.m4a")
        self.assertEqual(
            mp4_tag_atoms(first.path)[b"cnID"], [(21, (111).to_bytes(4, "big"))]
        )

        original_bytes = first.path.read_bytes()
        downloader._read_track.reset_mock()
        song.get_stream.reset_mock()
        duplicate = await downloader.download(song)
        self.assertEqual(duplicate.status, "skipped")
        self.assertEqual(duplicate.path, first.path)
        self.assertEqual(duplicate.bytes_written, 0)
        self.assertEqual(first.path.read_bytes(), original_bytes)
        downloader._read_track.assert_not_awaited()
        song.get_stream.assert_not_awaited()
        self.assertEqual(list(first.path.parent.glob("*.m4a")), [first.path])

        # Explicit replacement still works and never adds an ID suffix.
        replaced = await downloader.download(song, overwrite=True)
        self.assertEqual(replaced.status, "overwritten")
        self.assertEqual(replaced.path, first.path)
        self.assertEqual(list(first.path.parent.glob("*.m4a")), [first.path])

    async def test_auto_song_does_not_generate_id_collision_suffix(self):
        downloader, tag_resolver = self.downloader()
        downloader.path_formatter = PathFormatter(self.directory / "Library")
        song = self.song()
        tags = await tag_resolver.get_tags(song)
        expected = downloader.path_formatter.get_media_path(
            song, tags, extension=".m4a"
        )
        expected.parent.mkdir(parents=True)
        expected.write_bytes(downloader.muxer.remux(self.audio.read_bytes()))
        downloader.muxer.tag(expected, tags=Tags(title_id=222))
        original_bytes = expected.read_bytes()

        other = await downloader.download(song)
        self.assertEqual(other.status, "skipped")
        self.assertEqual(other.path, expected)
        self.assertEqual(expected.read_bytes(), original_bytes)
        downloader._read_track.reset_mock()
        self.assertEqual((await downloader.download(song)).status, "skipped")
        downloader._read_track.assert_not_awaited()
        self.assertEqual(list(expected.parent.glob("*.m4a")), [expected])
        replaced = await downloader.download(song, overwrite=True)
        self.assertEqual(replaced.path, expected)
        self.assertEqual(replaced.status, "overwritten")

    async def test_auto_library_song_matches_catalog_cnid(self):
        downloader, _ = self.downloader()
        downloader.path_formatter = PathFormatter(self.directory / "Library")
        song = Song(
            "i.library-song",
            api=Mock(),
            is_library=True,
            metadata={
                "id": "i.library-song",
                "relationships": {"catalog": {"data": [{"id": "12345"}]}},
            },
        )
        song.get_stream = AsyncMock(return_value=Mock(codec=SongCodec.AAC))
        song.get_cover = AsyncMock(return_value=None)
        song.get_lyrics = AsyncMock(return_value=None)
        first = await downloader.download(song)
        self.assertEqual(first.status, "downloaded")
        downloader._read_track.reset_mock()
        self.assertEqual((await downloader.download(song)).status, "skipped")
        downloader._read_track.assert_not_awaited()

    async def test_auto_paths_without_index_recognize_embedded_cnid(self):
        formatter = PathFormatter(self.directory / "Formatted")
        downloader = Downloader(path_formatter=formatter)
        self.mock_uploaded_download(downloader)
        video = self.uploaded_video()
        first = await downloader.download(video)
        self.assertEqual(first.status, "downloaded")
        # As in Gamdl 3.x, re-read the native cnID tag when no index exists.
        downloader._save_track.reset_mock()
        second = await downloader.download(video)
        self.assertEqual(second.status, "skipped")
        self.assertEqual(second.path, first.path)
        downloader._save_track.assert_not_awaited()

        other = await downloader.download(self.uploaded_video("5555555555"))
        self.assertEqual(other.status, "downloaded")
        self.assertEqual(other.path.name, "Original Upload 2.m4v")
        downloader._save_track.reset_mock()
        repeated = await downloader.download(self.uploaded_video("5555555555"))
        self.assertEqual(repeated.status, "skipped")
        self.assertEqual(repeated.path, other.path)
        downloader._save_track.assert_not_awaited()

    async def test_auto_video_collision_uses_next_number_and_reuses_gaps(self):
        formatter = PathFormatter(self.directory / "Numbered")
        downloader = Downloader(path_formatter=formatter)
        self.mock_uploaded_download(downloader)
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

        async def save(
            _media_id, _source, track, destination, _callback, *, event_callback=None
        ):
            shutil.copyfile(self.video, destination)
            return "none"

        downloader._save_track = AsyncMock(side_effect=save)
        actual_publish = original_publish_file
        raced = False

        def racing_publish(source, destination, *, overwrite):
            nonlocal raced
            if not raced:
                raced = True
                # Another process raced to claim the first candidate.
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"other video")
                return False
            return actual_publish(source, destination, overwrite=overwrite)

        with patch(
            "gamdl.download.downloader.publish_file", side_effect=racing_publish
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

        async def save(
            _media_id, _source, track, destination, _callback, *, event_callback=None
        ):
            shutil.copyfile(self.video, destination)
            return "none"

        downloader._save_track = AsyncMock(side_effect=save)

        raced = False

        def competing_publish(source, destination, *, overwrite):
            nonlocal raced
            if not raced:
                raced = True
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"other video")
                self.assertEqual(overwrite, "skip")
                return False
            return original_publish_file(source, destination, overwrite=overwrite)

        with patch(
            "gamdl.download.downloader.publish_file", side_effect=competing_publish
        ):
            result = await downloader.download(
                self.uploaded_video("555"), overwrite=True
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
