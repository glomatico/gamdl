"""Reusable numbered view over a cursor-backed, lazily loaded collection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

from gamdl.pagination import MediaPage

T = TypeVar("T")


class PaginatedChoices(Generic[T]):
    """Cache viewed pages; moving backward never performs network requests."""

    def __init__(self, load: Callable[[int], Awaitable[MediaPage[T] | None]]):
        self._load = load
        self.pages: dict[int, MediaPage[T]] = {}
        self.index = 0

    @property
    def current(self) -> MediaPage[T] | None:
        return self.pages.get(self.index)

    @property
    def has_next(self) -> bool:
        page = self.current
        return bool(page and page.next_cursor)

    async def fetch(self, index: int) -> MediaPage[T] | None:
        if index < 0:
            return None
        if index not in self.pages:
            page = await self._load(index)
            if page is None:
                return None
            self.pages[index] = page
        return self.pages[index]

    async def next(self) -> bool:
        if not self.has_next:
            return False
        if await self.fetch(self.index + 1) is None:
            return False
        self.index += 1
        return True

    def previous(self) -> bool:
        if self.index == 0:
            return False
        self.index -= 1
        return True

    async def load_all(self, on_page: Callable[[int], None] | None = None) -> None:
        """Load all remaining pages without moving the user's current page."""
        index = max(self.pages, default=-1)
        if index < 0:
            await self.fetch(0)
            index = 0
        while self.pages[index].next_cursor is not None:
            index += 1
            if on_page:
                on_page(index + 1)
            if await self.fetch(index) is None:
                raise ValueError("Pagination ended before advertised next page")
