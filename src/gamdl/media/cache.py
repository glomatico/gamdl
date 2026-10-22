"""Per-instance async memoization for lazy media and stream operations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


class InstanceCache:
    """Coalesce concurrent loads without keeping failures or canceled results.

    Cache ownership is explicit: a Media or AudioStream owns one instance.
    Invalidation detaches pending tasks so older requests cannot refill a
    freshly cleared cache. Canceling one waiter does not cancel the load.
    """

    def __init__(self) -> None:
        self.values: dict[object, Any] = {}
        self._inflight: dict[object, asyncio.Task[Any]] = {}

    def seed(self, key: object, value: Any) -> None:
        self.values[key] = value

    async def get(self, key: object, factory: Callable[[], Awaitable[T]]) -> T:
        if key in self.values:
            return self.values[key]
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(factory())
            self._inflight[key] = task

            def finish(done: asyncio.Task[Any]) -> None:
                try:
                    result = done.result()
                except BaseException:
                    successful = False
                else:
                    successful = True
                if self._inflight.get(key) is done:
                    self._inflight.pop(key, None)
                    if successful:
                        self.values[key] = result

            task.add_done_callback(finish)
        return await asyncio.shield(task)

    def clear(self) -> None:
        self.values.clear()
        self._inflight.clear()
