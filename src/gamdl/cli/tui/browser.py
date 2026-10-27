"""Reusable keyboard-driven, hierarchical terminal media browser.

The UI does not own API clients, media objects, or download logic. Browsing a
category materializes its choices once; selecting a whole category does not.
"""

from __future__ import annotations

import asyncio

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


class ArtistBrowserState:
    """Testable navigation/selection state shared by the terminal renderer."""

    def __init__(self, request: ArtistBrowseRequest):
        self.request = request
        self.category: SelectionKind | None = None
        self.cursor = 0
        self.root_cursor = 0
        self.loaded: dict[SelectionKind, tuple[CollectionOption, ...]] = {}
        # A category mapped to None means all items, even if never loaded.
        self.selected: dict[SelectionKind, set[int] | None] = {}
        self.loading = False
        self.error: str | None = None
        self._generation = 0

    @property
    def rows(self) -> int:
        return (
            len(self.request.categories) + 1
            if self.category is None
            else len(self.loaded.get(self.category, ())) + 1
        )

    def move(self, change: int):
        if not self.loading:
            self.cursor = max(0, min(self.rows - 1, self.cursor + change))

    def toggle(self):
        """Space toggles categories or individual item occurrence indices."""
        if self.loading or self.cursor == self.rows - 1:
            return
        self.error = None
        if self.category is None:
            kind = self.request.categories[self.cursor][0]
            if kind in self.selected:
                del self.selected[kind]
            else:
                self.selected[kind] = None
            return
        kind = self.category
        idx = self.loaded[kind][self.cursor].index
        current = self.selected.get(kind)
        if kind not in self.selected:
            current = set()
        elif current is None:
            current = {choice.index for choice in self.loaded[kind]}
        assert current is not None
        if idx in current:
            current.remove(idx)
        else:
            current.add(idx)
        if not current:
            self.selected.pop(kind, None)
        elif len(current) == len(self.loaded[kind]):
            self.selected[kind] = None
        else:
            self.selected[kind] = current

    def select_all(self):
        """Ctrl+A selects all currently visible items, without extra I/O."""
        if self.loading:
            return
        if self.category is None:
            for kind, _ in self.request.categories:
                self.selected[kind] = None
        elif self.loaded.get(self.category):
            self.selected[self.category] = None
        self.error = None

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
            if kind not in self.loaded:
                self.loaded[kind] = tuple(await self.request.load_items(kind))
            if generation == self._generation:
                self.category = kind
                self.cursor = 0
        except Exception as exc:
            # Never display raw endpoint URLs or credentials in terminal UI.
            if generation == self._generation:
                code = getattr(exc, "status_code", None)
                self.error = f"Unable to load category ({'HTTP ' + str(code) if code is not None else type(exc).__name__})"
        finally:
            self.loading = False

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
        return "[x]" if chosen is None else "[-]"

    def mark_item(self, index: int) -> str:
        assert self.category is not None
        kind = self.category
        if kind not in self.selected:
            return "[ ]"
        items = self.selected[kind]
        return "[x]" if items is None or index in items else "[ ]"

    def result(self) -> ArtistBrowseResult:
        return ArtistBrowseResult(
            tuple(
                ArtistCategorySelection(
                    kind,
                    None
                    if self.selected[kind] is None
                    else tuple(sorted(self.selected[kind])),
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
            rows = [
                (state.mark_item(item.index), item.title)
                for item in state.loaded.get(state.category, ())
            ]
        rows.append(("", "Download selected"))
        for i, (mark, label) in enumerate(rows):
            selected = i == state.cursor
            style = (
                "class:focused"
                if selected
                else ("class:action" if i == len(rows) - 1 else "")
            )
            fragments.append((style, f" {'❯' if selected else ' '} {mark:4} {label}\n"))
        if state.loading:
            fragments.append(("class:muted", "\n  Loading category...\n"))
        if state.error:
            fragments.append(("class:error", f"\n  {state.error}\n"))
        return FormattedText(fragments)

    def footer() -> FormattedText:
        summary = f"{len(state.selected)} categories selected"
        return menu_footer(
            summary + "  ·  ↑/↓ navigate  ·  Space mark  ·  Enter open/download",
            "Ctrl+A select all  ·  Esc/Backspace back  ·  Ctrl+C cancel",
        )

    app = make_app(
        body=body,
        footer=footer,
        cursor_row=lambda: state.cursor + 2,
        keys=kb,
        input=input,
        output=output,
    )

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

    @kb.add("space")
    def toggle(event):
        state.toggle()

    @kb.add("c-a")
    def select_all(event):
        state.select_all()

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
            task = asyncio.create_task(state.expand())
            tasks.add(task)
            task.add_done_callback(lambda t: (tasks.discard(t), app.invalidate()))

    @kb.add("escape")
    @kb.add("backspace")
    def back(event):
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
