"""Reusable keyboard-driven, hierarchical terminal media browser.

The UI does not own API clients, media objects, or download logic. Browsing a
category materializes its choices once; selecting a whole category does not.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import click
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings

from gamdl.media.models import (
    ArtistBrowseRequest,
    ArtistBrowseResult,
    ArtistCategorySelection,
    CollectionOption,
)
from gamdl.media.enums import SelectionKind

from .app import make_app
from .components import footer as menu_footer, heading
from .pagination import PaginatedChoices


class ArtistBrowserState:
    """Testable navigation/selection state shared by the terminal renderer."""

    def __init__(self, request: ArtistBrowseRequest):
        self.request = request
        self.category: SelectionKind | None = None
        self.cursor = 0
        self.root_cursor = 0
        self.loaded: dict[SelectionKind, PaginatedChoices[CollectionOption]] = {}
        # A category mapped to None means all items, even if never loaded.
        self.selected: dict[SelectionKind, set[int] | None] = {}
        # In all-selected mode, exclusions can reference any visited page.
        self.excluded: dict[SelectionKind, set[int]] = {}
        self.loading = False
        self.loading_page: int | None = None
        self.error: str | None = None
        self._generation = 0
        self.on_update: Callable[[], None] = lambda: None

    @property
    def rows(self) -> int:
        return (
            len(self.request.categories) + 1
            if self.category is None
            else len(self.items) + 1
        )

    @property
    def items(self) -> tuple[CollectionOption, ...]:
        if self.category is None:
            return ()
        page = self.loaded[self.category].current
        return page.items if page else ()

    @property
    def page_number(self) -> int:
        if self.category is None:
            return 0
        return self.loaded[self.category].index + 1

    def _collection(self, kind: SelectionKind) -> PaginatedChoices[CollectionOption]:
        if kind not in self.loaded:
            self.loaded[kind] = PaginatedChoices(
                lambda index: self.request.load_page(kind, index)
            )
        return self.loaded[kind]

    def move(self, change: int):
        if not self.loading:
            self.cursor = max(0, min(self.rows - 1, self.cursor + change))

    def clear(self):
        """Deselect the current category or all categories without fetching."""
        if self.loading:
            return
        if self.category is None:
            self.selected.clear()
            self.excluded.clear()
        else:
            self.selected.pop(self.category, None)
            self.excluded.pop(self.category, None)
        self.error = None

    def toggle(self):
        """Space toggles categories or individual item occurrence indices."""
        if self.loading or self.cursor == self.rows - 1:
            return
        self.error = None
        if self.category is None:
            kind = self.request.categories[self.cursor][0]
            if kind in self.selected:
                del self.selected[kind]
                self.excluded.pop(kind, None)
            else:
                self.selected[kind] = None
                self.excluded.pop(kind, None)
            return
        kind = self.category
        idx = self.items[self.cursor].index
        current = self.selected.get(kind)
        if kind in self.selected and current is None:
            excluded = self.excluded.setdefault(kind, set())
            if idx in excluded:
                excluded.remove(idx)
            else:
                excluded.add(idx)
        else:
            if current is None:
                current = set()
            if idx in current:
                current.remove(idx)
            else:
                current.add(idx)
            if current:
                self.selected[kind] = current
            else:
                self.selected.pop(kind, None)

    async def select_all(self):
        """Select every item lazily, including pages not visited yet."""
        if self.loading:
            return
        if self.category is None:
            for kind, _ in self.request.categories:
                self.selected[kind] = None
                self.excluded.pop(kind, None)
        else:
            self.selected[self.category] = None
            self.excluded.pop(self.category, None)
        self.error = None

    def _loading_page(self, page: int):
        self.loading_page = page
        self.on_update()

    @staticmethod
    def _error_message(exc: Exception) -> str:
        code = getattr(exc, "status_code", None)
        return f"Unable to load page ({'HTTP ' + str(code) if code is not None else type(exc).__name__})"

    async def expand(self):
        """Enter on a root category expands it; never triggers a download."""
        if self.loading or self.category is not None or self.cursor == self.rows - 1:
            return
        kind = self.request.categories[self.cursor][0]
        self.root_cursor = self.cursor
        self.loading = True
        self.error = None
        generation = self._generation
        try:
            collection = self._collection(kind)
            await collection.fetch(collection.index)
            if generation == self._generation:
                self.category = kind
                self.cursor = 0
        except Exception as exc:
            if generation == self._generation:
                self.error = self._error_message(exc)
        finally:
            self.loading = False

    async def next_page(self):
        if self.loading or self.category is None:
            return
        collection = self.loaded[self.category]
        if not collection.has_next:
            return
        self.loading = True
        self.loading_page = collection.index + 2
        try:
            if await collection.next():
                self.cursor = 0
                self.error = None
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.error = self._error_message(exc)
        finally:
            self.loading = False
            self.loading_page = None

    def previous_page(self):
        if self.category is not None and not self.loading:
            if self.loaded[self.category].previous():
                self.cursor = 0
                self.error = None

    def back(self) -> bool:
        if self.category is None and not self.loading:
            return False
        self._generation += 1
        self.category = None
        self.cursor = self.root_cursor
        self.error = None
        return True

    def mark(self, kind: SelectionKind) -> str:
        if kind not in self.selected:
            return "[ ]"
        chosen = self.selected[kind]
        return "[x]" if chosen is None and not self.excluded.get(kind) else "[-]"

    def mark_item(self, index: int) -> str:
        assert self.category is not None
        kind = self.category
        if kind not in self.selected:
            return "[ ]"
        items = self.selected[kind]
        return (
            "[x]"
            if (
                index not in self.excluded.get(kind, set())
                if items is None
                else index in items
            )
            else "[ ]"
        )

    def result(self) -> ArtistBrowseResult:
        return ArtistBrowseResult(
            tuple(
                ArtistCategorySelection(
                    kind,
                    None
                    if self.selected[kind] is None
                    else tuple(sorted(self.selected[kind])),
                    tuple(sorted(self.excluded.get(kind, ()))),
                )
                for kind, _ in self.request.categories
                if kind in self.selected
            )
        )


async def browse_artist(
    request: ArtistBrowseRequest, *, input=None, output=None
) -> ArtistBrowseResult:
    """Run full-screen Prompt Toolkit UI with explicit Download selected action."""
    state = ArtistBrowserState(request)
    kb = KeyBindings()
    tasks: set[asyncio.Task] = set()

    def body() -> FormattedText:
        fragments: list[tuple[str, str]] = []
        subtitle = (
            "Categories"
            if state.category is None
            else next(
                label for kind, label in request.categories if kind == state.category
            )
        )
        fragments.extend(heading(request.artist_name, subtitle))
        if state.category is None:
            rows = [
                (state.mark(kind), f"{title}  ›") for kind, title in request.categories
            ]
        else:
            rows = [(state.mark_item(item.index), item.title) for item in state.items]
            has_next = state.loaded[state.category].has_next
            page_hint = (
                f"Loading page {state.loading_page}... Esc cancels"
                if state.loading
                else f"{'→ Next page' if has_next else 'Last page'}  ·  ← Previous page"
            )
            fragments.append(
                (
                    "class:muted",
                    f" Page {state.page_number}  ·  {page_hint}\n",
                )
            )
        rows.append(("", "Download selected"))
        for i, (mark, label) in enumerate(rows):
            selected = i == state.cursor
            style = (
                "class:focused"
                if selected
                else ("class:action" if i == len(rows) - 1 else "")
            )
            fragments.append((style, f" {'❯' if selected else ' '} {mark:4} {label}\n"))
        if state.loading and state.category is None:
            fragments.append(
                (
                    "class:muted",
                    f"\n  Loading page {state.loading_page or 1}... (Esc to cancel)\n",
                )
            )
        if state.error:
            fragments.append(("class:error", f"\n  {state.error}\n"))
        return FormattedText(fragments)

    def footer() -> FormattedText:
        summary = f"{len(state.selected)} categories selected"
        return menu_footer(
            summary
            + "  ·  ↑/↓ navigate  ·  ←/→ pages  ·  Space mark  ·  Enter open/download",
            "Ctrl+A select all  ·  Ctrl+D clear  ·  PgUp/PgDn jump  ·  Esc back  ·  Ctrl+C cancel",
        )

    app = make_app(
        body=body,
        footer=footer,
        cursor_row=lambda: state.cursor + (3 if state.category is not None else 2),
        keys=kb,
        input=input,
        output=output,
    )
    state.on_update = app.invalidate

    @kb.add("up")
    @kb.add("k")
    def up(event):
        state.move(-1)

    @kb.add("down")
    @kb.add("j")
    def down(event):
        state.move(1)

    @kb.add("home")
    def home(event):
        if not state.loading:
            state.cursor = 0

    @kb.add("end")
    def end(event):
        if not state.loading:
            state.cursor = state.rows - 1

    @kb.add("pageup")
    def pageup(event):
        state.move(-10)

    @kb.add("pagedown")
    def pagedown(event):
        state.move(10)

    @kb.add("space")
    def toggle(event):
        state.toggle()

    @kb.add("c-a")
    def select_all(event):
        start_task(state.select_all())

    @kb.add("c-d")
    def clear(event):
        state.clear()

    def start_task(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.add(task)
        task.add_done_callback(lambda t: (tasks.discard(t), app.invalidate()))

    @kb.add("right")
    def right(event):
        if state.category is not None and not state.loading:
            start_task(state.next_page())

    @kb.add("left")
    def left(event):
        state.previous_page()

    @kb.add("enter")
    def enter(event):
        if state.loading:
            return
        if state.cursor == state.rows - 1:
            if not state.selected:
                state.error = "Select at least one item before downloading"
                return
            app.exit(result=state.result())
        elif state.category is not None:
            state.toggle()
        else:
            start_task(state.expand())

    @kb.add("escape")
    @kb.add("backspace")
    def back(event):
        if state.loading:
            for task in tuple(tasks):
                task.cancel()
            return
        if not state.back():
            app.exit(result=None)

    @kb.add("c-c")
    def cancel(event):
        app.exit(result=None)

    try:
        result = await app.run_async()
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    if result is None:
        raise click.Abort()
    return result
