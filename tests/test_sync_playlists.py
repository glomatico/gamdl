"""Offline playlist synchronization: ordering, duplicates and safe failure paths."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn

from gamdl.cli.commands.sync.playlists import _run
from gamdl.cli.config.models import AppConfig
from gamdl.cli.progress import RipProgress
from gamdl.download import DownloadResult, MediaStatus
from gamdl.sync.playlists import (
    LocalMediaIndex,
    PlaylistSnapshot,
    PlaylistSyncError,
    plan_playlist,
    publish_playlist,
    remote_track,
)


def resource(media_id: str, kind: str = "songs") -> dict:
    return {"type": kind, "id": media_id, "attributes": {"name": media_id}}


PLAYLIST_ID = "pl." + "a" * 32
OTHER_PLAYLIST_ID = "pl." + "b" * 32


class FakeAPI:
    def __init__(
        self,
        pages,
        fail_after: int | None = None,
        pages_by_playlist: dict | None = None,
    ):
        self.pages = pages
        self.fail_after = fail_after
        self.pages_by_playlist = pages_by_playlist or {PLAYLIST_ID: pages}
        self.storefront = "br"

    async def iter_playlist_tracks(self, playlist_id, *, is_library=False):
        for i, item in enumerate(self.pages_by_playlist[playlist_id]):
            if self.fail_after is not None and i == self.fail_after:
                raise RuntimeError("Second API page failed")
            yield item


class FakeItunes:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class PlaylistSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "index.sqlite3"
        self.playlist = self.root / "Playlists/Editor/List.m3u8"
        self.playlist.parent.mkdir(parents=True)
        self.media = {}
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(
                "CREATE TABLE media_files (path TEXT, media_type TEXT, "
                "media_id TEXT, file_size INTEGER)"
            )

    def add_media(self, media_id: str, name: str, kind: str = "songs") -> Path:
        file = self.root / "Artist/Album" / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"fake-media-" + media_id.encode())
        self.media[media_id] = file
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(
                "INSERT INTO media_files VALUES (?,?,?,?)",
                (str(file), kind, media_id, file.stat().st_size),
            )
        return file

    def set_playlist(self, *names: str, ending="\n", bom=b"", extinf=False):
        lines = [
            "#EXTM3U",
            "#PLAYLIST:Legacy Title",
            "#Gamdl-PLAYLIST-ARTIST:Original Curator",
            f"#Gamdl-APPLE-MUSIC-ID:{PLAYLIST_ID}",
        ]
        for name in names:
            if extinf:
                lines.append(f"#EXTINF:150,{name}")
            path = self.media[name]
            lines.append(str(path.relative_to(self.root)))  # Rebased below.
            lines[-1] = str(Path("../../") / lines[-1])
        self.playlist.write_bytes(bom + (ending.join(lines) + ending).encode())

    def second_playlist(self) -> Path:
        other = self.root / "Playlists/Editor/Second.m3u8"
        other.write_bytes(
            self.playlist.read_bytes()
            .replace(PLAYLIST_ID.encode(), OTHER_PLAYLIST_ID.encode())
            .replace(b"#PLAYLIST:Legacy Title", b"#PLAYLIST:Second Playlist")
        )
        return other

    def snapshot(self):
        return PlaylistSnapshot.read(self.playlist, self.root)

    def index(self):
        return LocalMediaIndex(self.root, self.db)

    def test_download_batch_progress_tracks_completed_total(self):
        stream = StringIO()
        output = Console(file=stream, width=110, color_system=None)
        with RipProgress(enabled=True, max_visible=3, console=output) as progress:
            progress.start_batch(2)
            summary = next(
                task for task in progress.progress.tasks if task.fields.get("discovery")
            )
            self.assertEqual(summary.total, 2)
            self.assertEqual(summary.completed, 0)
            progress.add(1, "First")
            progress.finish(1, "First", "downloaded")
            self.assertEqual(summary.completed, 1)
            self.assertEqual(summary.description, "Downloading 1/2 tracks")
            progress.add(2, "Second")
            progress.finish(2, "Second", "failed", "Not available")
            self.assertEqual(summary.completed, 2)
            self.assertEqual(summary.description, "Downloading 2/2 tracks")

    async def test_playlist_check_progress_advances_after_success_and_failure(self):
        self.add_media("101", "01 First.m4a")
        self.set_playlist("101")
        second = self.second_playlist()
        api = FakeAPI([], pages_by_playlist={PLAYLIST_ID: [resource("101")]})

        async def fake_auth(*args, **kwargs):
            return api, None

        class TrackingProgress(Progress):
            instance = None

            def __init__(self, *columns, **kwargs):
                super().__init__(*columns, **kwargs)
                self.checkpoints = []
                self.columns_received = columns
                TrackingProgress.instance = self

            def advance(self, task_id, advance=1):
                super().advance(task_id, advance)
                self.checkpoints.append(self.tasks[0].completed)

        output = Console(file=StringIO(), width=110, color_system=None)
        original = AppConfig()
        settings = replace(
            original, database=replace(original.database, path=str(self.db))
        )
        with (
            patch(
                "gamdl.cli.commands.sync.playlists.authenticate",
                side_effect=fake_auth,
            ),
            patch("gamdl.cli.commands.sync.playlists.console", return_value=output),
            patch("gamdl.cli.commands.sync.playlists.Progress", TrackingProgress),
        ):
            result = await _run(
                settings,
                root=self.root,
                selected=(self.playlist, second),
                limit=None,
                dry_run=True,
                no_download=False,
                yes=False,
            )
        self.assertEqual(result, 1)  # Second playlist has no remote response.
        progress = TrackingProgress.instance
        self.assertIsNotNone(progress)
        self.assertEqual(progress.tasks[0].total, 2)
        self.assertEqual(progress.checkpoints, [1, 2])
        self.assertTrue(
            any(isinstance(c, BarColumn) for c in progress.columns_received)
        )
        self.assertTrue(
            any(isinstance(c, MofNCompleteColumn) for c in progress.columns_received)
        )
        self.assertFalse(
            any(isinstance(c, SpinnerColumn) for c in progress.columns_received)
        )

    def test_removal_reordering_and_duplicates_keep_original_media(self):
        a = self.add_media("101", "01 First.m4a")
        b = self.add_media("102", "02 Second.m4a")
        c = self.add_media("103", "03 Third.m4a")
        self.set_playlist(
            "101", "102", "101", ending="\r\n", bom=b"\xef\xbb\xbf", extinf=True
        )
        index = self.index()
        snapshot = self.snapshot()
        tracks = tuple(
            map(
                remote_track,
                [resource("102"), resource("101"), resource("103"), resource("101")],
            )
        )
        plan = plan_playlist(snapshot, tracks, index)
        self.assertEqual((plan.added, plan.removed), (1, 0))
        self.assertTrue(plan.reordered)
        self.assertEqual(plan.missing, ())
        self.assertTrue(publish_playlist(plan, index))
        content = self.playlist.read_bytes()
        self.assertTrue(content.startswith(b"\xef\xbb\xbf#EXTM3U\r\n"))
        self.assertIn(b"#PLAYLIST:Legacy Title\r\n", content)
        self.assertIn(b"#Gamdl-PLAYLIST-ARTIST:Original Curator\r\n", content)
        after = self.snapshot()
        self.assertEqual(
            [index.identity(e.path) for e in after.entries], [t.key for t in tracks]
        )
        self.assertEqual(sum(len(e.comments) for e in after.entries), 3)
        self.assertTrue(all(f.exists() for f in (a, b, c)))
        self.assertFalse(publish_playlist(plan_playlist(after, tracks, index), index))

    def test_removed_local_track_stays_on_disk(self):
        a = self.add_media("101", "01 First.m4a")
        b = self.add_media("102", "02 Second.m4a")
        self.set_playlist("101", "102")
        idx = self.index()
        plan = plan_playlist(self.snapshot(), (remote_track(resource("101")),), idx)
        self.assertEqual((plan.added, plan.removed), (0, 1))
        self.assertTrue(publish_playlist(plan, idx))
        self.assertEqual([e.path for e in self.snapshot().entries], [a])
        self.assertTrue(b.is_file())

    def test_missing_new_track_does_not_publish(self):
        self.add_media("101", "01 First.m4a")
        self.set_playlist("101")
        idx = self.index()
        plan = plan_playlist(
            self.snapshot(),
            (remote_track(resource("101")), remote_track(resource("999"))),
            idx,
        )
        before = self.playlist.read_bytes()
        self.assertEqual(len(plan.missing), 1)
        with self.assertRaises(PlaylistSyncError):
            publish_playlist(plan, idx)
        self.assertEqual(self.playlist.read_bytes(), before)

    def test_empty_response_and_unidentified_old_track_are_rejected(self):
        self.add_media("101", "01 First.m4a")
        self.set_playlist("101")
        snapshot = self.snapshot()
        with self.assertRaisesRegex(PlaylistSyncError, "empty API response"):
            plan_playlist(snapshot, (), self.index())
        with self.assertRaisesRegex(PlaylistSyncError, "readable media ID"):
            plan_playlist(
                snapshot,
                (remote_track(resource("101")),),
                LocalMediaIndex(self.root, None),
            )

    def test_incomplete_remote_data_and_unsupported_tracks_rejected(self):
        with self.assertRaisesRegex(PlaylistSyncError, "Unsupported track type"):
            remote_track(resource("abc", "stations"))
        with self.assertRaisesRegex(PlaylistSyncError, "missing a media ID"):
            remote_track({"type": "songs"})

    def test_existing_file_changed_since_snapshot_refuses_publish(self):
        self.add_media("101", "01 First.m4a")
        self.add_media("102", "02 Second.m4a")
        self.set_playlist("101", "102")
        plan = plan_playlist(
            self.snapshot(), (remote_track(resource("101")),), self.index()
        )
        self.playlist.write_bytes(self.playlist.read_bytes() + b"# user edited\n")
        with self.assertRaisesRegex(PlaylistSyncError, "edited during"):
            publish_playlist(plan, self.index())

    async def test_dry_run_never_writes_or_instantiates_drm(self):
        self.add_media("101", "01 First.m4a")
        self.set_playlist("101")
        api = FakeAPI([resource("101"), resource("999")])

        async def fake_auth(*args, **kwargs):
            return api, None

        original = AppConfig()
        settings = replace(
            original,
            database=replace(original.database, path=str(self.db)),
            output=replace(original.output, directory=str(self.root)),
        )
        before = self.playlist.read_bytes()
        with (
            patch(
                "gamdl.cli.commands.sync.playlists.authenticate", side_effect=fake_auth
            ),
            patch("gamdl.cli.commands.sync.playlists.create_drm") as drm,
        ):
            code = await _run(
                settings,
                root=self.root,
                selected=(self.playlist,),
                limit=None,
                dry_run=True,
                no_download=False,
                yes=False,
            )
        self.assertEqual(code, 0)
        drm.assert_not_called()
        self.assertEqual(before, self.playlist.read_bytes())

    async def test_default_summary_hides_track_details_but_verbose_shows_them(self):
        self.add_media("101", "01 First.m4a")
        self.set_playlist("101")
        api = FakeAPI([resource("101"), resource("999")])

        async def fake_auth(*args, **kwargs):
            return api, None

        original = AppConfig()
        settings = replace(
            original, database=replace(original.database, path=str(self.db))
        )

        async def capture(verbose):
            stream = StringIO()
            capture_console = Console(file=stream, width=110, color_system=None)
            with (
                patch(
                    "gamdl.cli.commands.sync.playlists.authenticate",
                    side_effect=fake_auth,
                ),
                patch(
                    "gamdl.cli.commands.sync.playlists.console",
                    return_value=capture_console,
                ),
            ):
                code = await _run(
                    settings,
                    root=self.root,
                    selected=(self.playlist,),
                    limit=None,
                    dry_run=True,
                    no_download=False,
                    yes=False,
                    verbose=verbose,
                )
            self.assertEqual(code, 0)
            return stream.getvalue()

        compact = await capture(False)
        self.assertIn("1 need updates", compact)
        self.assertIn("Legacy Title", compact)
        self.assertIn("Playlists needing attention", compact)
        self.assertNotIn("    + 999 (999)", compact)
        detailed = await capture(True)
        self.assertIn("    + 999 (999)", detailed)
        self.assertIn("Playlists needing attention", detailed)

    async def test_up_to_date_playlist_shows_no_changes_table(self):
        self.add_media("101", "01 First.m4a")
        self.set_playlist("101")
        api = FakeAPI([resource("101")])

        async def fake_auth(*args, **kwargs):
            return api, None

        original = AppConfig()
        settings = replace(
            original, database=replace(original.database, path=str(self.db))
        )
        stream = StringIO()
        with (
            patch(
                "gamdl.cli.commands.sync.playlists.authenticate", side_effect=fake_auth
            ),
            patch(
                "gamdl.cli.commands.sync.playlists.console",
                return_value=Console(file=stream, width=110, color_system=None),
            ),
        ):
            code = await _run(
                settings,
                root=self.root,
                selected=(self.playlist,),
                limit=None,
                dry_run=True,
                no_download=False,
                yes=False,
            )
        self.assertEqual(code, 0)
        self.assertIn("All local playlists are up to date", stream.getvalue())
        self.assertNotIn("Playlists needing attention", stream.getvalue())

    async def test_no_download_missing_remote_track_keeps_playlist(self):
        self.add_media("101", "01 First.m4a")
        self.set_playlist("101")
        api = FakeAPI([resource("101"), resource("999")])

        async def fake_auth(*args, **kwargs):
            return api, None

        original = AppConfig()
        settings = replace(
            original, database=replace(original.database, path=str(self.db))
        )
        before = self.playlist.read_bytes()
        with patch(
            "gamdl.cli.commands.sync.playlists.authenticate", side_effect=fake_auth
        ):
            code = await _run(
                settings,
                root=self.root,
                selected=(self.playlist,),
                limit=None,
                dry_run=False,
                no_download=True,
                yes=False,
            )
        self.assertEqual(code, 1)
        self.assertEqual(before, self.playlist.read_bytes())

    async def test_pagination_failure_does_not_publish_partial_playlist(self):
        self.add_media("101", "01 First.m4a")
        self.set_playlist("101")
        api = FakeAPI([resource("101"), resource("999")], fail_after=1)

        async def fake_auth(*args, **kwargs):
            return api, None

        original = AppConfig()
        settings = replace(
            original, database=replace(original.database, path=str(self.db))
        )
        before = self.playlist.read_bytes()
        with patch(
            "gamdl.cli.commands.sync.playlists.authenticate", side_effect=fake_auth
        ):
            code = await _run(
                settings,
                root=self.root,
                selected=(self.playlist,),
                limit=None,
                dry_run=False,
                no_download=True,
                yes=False,
            )
        self.assertEqual(code, 1)
        self.assertEqual(before, self.playlist.read_bytes())

    async def test_downloads_only_missing_track_and_publishes_after_completion(self):
        self.add_media("101", "01 First.m4a")
        self.set_playlist("101")
        new = self.root / "Artist/Album/02 New.m4a"
        api = FakeAPI([resource("101"), resource("999")])

        async def fake_auth(*args, **kwargs):
            return api, None

        async def fake_download(media, **kwargs):
            self.assertEqual(media.id, "999")
            self.assertFalse(kwargs["save_playlist"])
            new.write_bytes(b"downloaded-999")
            return DownloadResult("999", new, MediaStatus.DOWNLOADED, 14, None)

        original = AppConfig()
        settings = replace(
            original, database=replace(original.database, path=str(self.db))
        )
        before = self.playlist.read_bytes()
        downloader = AsyncMock()
        downloader.download.side_effect = fake_download
        with (
            patch(
                "gamdl.cli.commands.sync.playlists.authenticate", side_effect=fake_auth
            ),
            patch(
                "gamdl.cli.commands.sync.playlists.create_drm",
                new_callable=AsyncMock,
            ) as drm,
            patch(
                "gamdl.cli.commands.sync.playlists.ItunesApi.create",
                new_callable=AsyncMock,
                return_value=FakeItunes(),
            ),
            patch(
                "gamdl.cli.commands.sync.playlists.Downloader", return_value=downloader
            ),
        ):
            code = await _run(
                settings,
                root=self.root,
                selected=(self.playlist,),
                limit=None,
                dry_run=False,
                no_download=False,
                yes=False,
            )
        self.assertEqual(code, 0)
        drm.assert_awaited_once()
        self.assertNotEqual(self.playlist.read_bytes(), before)
        self.assertEqual(
            [e.path for e in self.snapshot().entries],
            [self.media["101"], new],
        )

    async def test_failed_download_keeps_playlist_unchanged(self):
        self.add_media("101", "01 First.m4a")
        self.set_playlist("101")
        api = FakeAPI([resource("101"), resource("999")])

        async def fake_auth(*args, **kwargs):
            return api, None

        downloader = AsyncMock()
        downloader.download.side_effect = RuntimeError("Stream unavailable")
        original = AppConfig()
        settings = replace(
            original, database=replace(original.database, path=str(self.db))
        )
        before = self.playlist.read_bytes()
        with (
            patch(
                "gamdl.cli.commands.sync.playlists.authenticate", side_effect=fake_auth
            ),
            patch(
                "gamdl.cli.commands.sync.playlists.create_drm",
                new_callable=AsyncMock,
            ),
            patch(
                "gamdl.cli.commands.sync.playlists.ItunesApi.create",
                new_callable=AsyncMock,
                return_value=FakeItunes(),
            ),
            patch(
                "gamdl.cli.commands.sync.playlists.Downloader", return_value=downloader
            ),
        ):
            code = await _run(
                settings,
                root=self.root,
                selected=(self.playlist,),
                limit=None,
                dry_run=False,
                no_download=False,
                yes=False,
            )
        self.assertEqual(code, 1)
        self.assertEqual(before, self.playlist.read_bytes())

    async def test_downloads_are_concurrent_and_shared_ids_only_download_once(self):
        self.add_media("101", "01 First.m4a")
        self.set_playlist("101")
        other = self.second_playlist()
        api = FakeAPI(
            [],
            pages_by_playlist={
                PLAYLIST_ID: [resource("101"), resource("999"), resource("777")],
                OTHER_PLAYLIST_ID: [resource("101"), resource("777"), resource("888")],
            },
        )

        async def fake_auth(*args, **kwargs):
            return api, None

        running = 0
        maximum = 0
        overlap = asyncio.Event()
        downloads = []

        async def fake_download(media, **kwargs):
            nonlocal running, maximum
            self.assertIsNotNone(kwargs["event_callback"])
            self.assertFalse(kwargs["save_playlist"])
            downloads.append(media.id)
            running += 1
            maximum = max(maximum, running)
            if running == 2:
                overlap.set()
            await asyncio.wait_for(overlap.wait(), timeout=1)
            await asyncio.sleep(0.01)
            running -= 1
            path = self.root / f"Artist/Album/{media.id}.m4a"
            path.write_bytes(b"new-" + media.id.encode())
            return DownloadResult(media.id, path, MediaStatus.DOWNLOADED, 7, None)

        original = AppConfig()
        settings = replace(
            original,
            database=replace(original.database, path=str(self.db)),
            download=replace(
                original.download, concurrent_downloads=2, segment_concurrency=6
            ),
            ui=replace(original.ui, progress=False),
        )
        downloader = AsyncMock()
        downloader.download.side_effect = fake_download
        with (
            patch(
                "gamdl.cli.commands.sync.playlists.authenticate", side_effect=fake_auth
            ),
            patch(
                "gamdl.cli.commands.sync.playlists.create_drm",
                new_callable=AsyncMock,
            ),
            patch(
                "gamdl.cli.commands.sync.playlists.ItunesApi.create",
                new_callable=AsyncMock,
                return_value=FakeItunes(),
            ),
            patch(
                "gamdl.cli.commands.sync.playlists.Downloader", return_value=downloader
            ) as constructor,
        ):
            code = await _run(
                settings,
                root=self.root,
                selected=(self.playlist, other),
                limit=None,
                dry_run=False,
                no_download=False,
                yes=False,
            )
        self.assertEqual(code, 0)
        self.assertEqual(maximum, 2)
        self.assertCountEqual(downloads, ["999", "777", "888"])
        self.assertEqual(constructor.call_args.kwargs["concurrency"], 6)
        self.assertEqual(
            len(PlaylistSnapshot.read(self.playlist, self.root).entries), 3
        )
        self.assertEqual(len(PlaylistSnapshot.read(other, self.root).entries), 3)

    async def test_failed_track_only_blocks_playlists_that_need_it(self):
        self.add_media("101", "01 First.m4a")
        self.set_playlist("101")
        other = self.second_playlist()
        api = FakeAPI(
            [],
            pages_by_playlist={
                PLAYLIST_ID: [resource("101"), resource("999"), resource("777")],
                OTHER_PLAYLIST_ID: [resource("101"), resource("777")],
            },
        )

        async def fake_auth(*args, **kwargs):
            return api, None

        async def fake_download(media, **kwargs):
            if media.id == "999":
                raise RuntimeError("Not streamable")
            new = self.root / f"Artist/Album/{media.id}.m4a"
            new.write_bytes(b"new-" + media.id.encode())
            return DownloadResult(media.id, new, MediaStatus.DOWNLOADED, 7, None)

        original = AppConfig()
        settings = replace(
            original,
            database=replace(original.database, path=str(self.db)),
            ui=replace(original.ui, progress=False),
        )
        before = self.playlist.read_bytes()
        downloader = AsyncMock()
        downloader.download.side_effect = fake_download
        with (
            patch(
                "gamdl.cli.commands.sync.playlists.authenticate", side_effect=fake_auth
            ),
            patch(
                "gamdl.cli.commands.sync.playlists.create_drm",
                new_callable=AsyncMock,
            ),
            patch(
                "gamdl.cli.commands.sync.playlists.ItunesApi.create",
                new_callable=AsyncMock,
                return_value=FakeItunes(),
            ),
            patch(
                "gamdl.cli.commands.sync.playlists.Downloader", return_value=downloader
            ),
        ):
            code = await _run(
                settings,
                root=self.root,
                selected=(self.playlist, other),
                limit=None,
                dry_run=False,
                no_download=False,
                yes=False,
            )
        self.assertEqual(code, 1)
        self.assertEqual(self.playlist.read_bytes(), before)
        self.assertEqual(len(PlaylistSnapshot.read(other, self.root).entries), 2)
