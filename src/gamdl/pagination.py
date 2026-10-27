"""Cursor-backed pages shared by media discovery and terminal browsers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class MediaPage(Generic[T]):
    """One API page. No total page count is assumed."""

    items: tuple[T, ...]
    next_cursor: str | None = None


class PageCache(Generic[T]):
    """Load cursor-based pages once, sequentially, for stable occurrence indices.

    Its loader receives a cursor rather than a page number; callers ask for a
    page number and this object follows/retains only the cursors it needs.
    """

    def __init__(self, load: Callable[[str | None], Awaitable[MediaPage[T]]]):
        self.load = load
        self.pages: list[MediaPage[T]] = []
        self._seen: set[str] = set()

    async def get(self, index: int) -> MediaPage[T] | None:
        if index < 0:
            raise ValueError("Page index must be nonnegative")
        while len(self.pages) <= index:
            if self.pages and self.pages[-1].next_cursor is None:
                return None
            cursor = self.pages[-1].next_cursor if self.pages else None
            if cursor is not None and cursor in self._seen:
                raise ValueError("Repeated pagination cursor")
            page = await self.load(cursor)
            if not isinstance(page, MediaPage):
                raise TypeError("Pagination loader must return MediaPage")
            if page.next_cursor is not None and (
                not isinstance(page.next_cursor, str) or not page.next_cursor
            ):
                raise ValueError("Invalid pagination cursor")
            if page.next_cursor is not None and (
                page.next_cursor == cursor or page.next_cursor in self._seen
            ):
                raise ValueError("Repeated pagination cursor")
            if cursor is not None:
                self._seen.add(cursor)
            self.pages.append(page)
        return self.pages[index]

    def offset(self, index: int) -> int:
        return sum(len(page.items) for page in self.pages[:index])

    async def iter_items(self) -> AsyncIterator[tuple[int, T]]:
        index = 0
        while (page := await self.get(index)) is not None:
            start = self.offset(index)
            for offset, item in enumerate(page.items):
                yield start + offset, item
            index += 1
