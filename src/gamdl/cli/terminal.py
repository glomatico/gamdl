"""Exclusive terminal selection sessions, independent of download concurrency."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from typing import TypeVar

from .progress import RipProgress

T = TypeVar("T")


class SelectionLease:
    """A release-once permit that is owned by one asyncio task."""

    def __init__(self, controller: TerminalController, owner: asyncio.Task):
        self.controller = controller
        self.owner = owner
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        if asyncio.current_task() is not self.owner:
            raise RuntimeError("Only the selection owner may release the terminal")
        self.released = True
        self.controller._owner = None
        self.controller._lock.release()
        self.controller._schedule_resume()


class TerminalController:
    """Only selection sessions may pause/resume Rich or open PTK menus.

    A download worker holds the selection permit through the entire
    source/codec (or video/audio) stage and releases it on the stream-stage
    completion event. It then downloads in parallel with subsequent sessions.
    Workers outside the session only update Rich's in-memory task state.
    """

    def __init__(self, progress: RipProgress) -> None:
        self.progress = progress
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._waiting = 0
        self._resume_handle: asyncio.Handle | None = None

    def _schedule_resume(self) -> None:
        # Transfer the terminal directly to a queued selector. Starting Rich
        # between two alternate screens flashes and can disturb cursor state.
        if self._resume_handle is None:
            self._resume_handle = asyncio.get_running_loop().call_soon(self._resume)

    def _resume(self) -> None:
        self._resume_handle = None
        if self._owner is None and not self._waiting:
            self.progress.resume_after_prompt()

    async def acquire(self) -> SelectionLease:
        self._waiting += 1
        try:
            await self._lock.acquire()
        except BaseException:
            self._schedule_resume()
            raise
        finally:
            self._waiting -= 1
        owner = asyncio.current_task()
        assert owner is not None
        self._owner = owner
        return SelectionLease(self, owner)

    @asynccontextmanager
    async def session(self):
        lease = await self.acquire()
        try:
            yield lease
        finally:
            lease.release()

    async def prompt(self, operation: Awaitable[T]) -> T:
        if asyncio.current_task() is not self._owner:
            raise RuntimeError("A full-screen prompt requires terminal ownership")
        self.progress.hide_for_prompt()
        return await operation
