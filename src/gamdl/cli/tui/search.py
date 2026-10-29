"""Full-screen, multi-category Apple Music search browser.

Search categories are navigation only: they cannot be selected wholesale.
Selections are individual result occurrences, retained across categories and
cached pages until the user explicitly chooses Download selected.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import click
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings

from gamdl.pagination import PageCache

from .app import make_app
from .components import footer, heading

SEARCH_CATEGORIES = (
    ("songs", "Songs"),
    ("music-videos", "Music videos"),
    ("uploaded-videos", "Uploaded videos"),
)


class SearchBrowserState:
    """Navigation and per-category selections, independent of the terminal."""

    def __init__(
        self,
        caches: Mapping[str, PageCache[dict]],
        *,
        media_type: str | None = None,
    ) -> None:
        if media_type is not None and media_type not in caches:
            raise ValueError("Search category is not available")
        self.caches = caches
        self.fixed_type = media_type is not None
        self.category = media_type
        self.root_cursor = 0
        self.cursor = 0
        self.pages = {kind: 0 for kind in caches}
        self.positions = {kind: 0 for kind in caches}
        self.selected = {kind: set() for kind in caches}
        self.loading = False
        self.error: str | None = None

    @property
    def categories(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (kind, title) for kind, title in SEARCH_CATEGORIES if kind in self.caches
        )

    @property
    def items(self) -> tuple[dict, ...]:
        if self.category is None:
            return ()
        cache = self.caches[self.category]
        index = self.pages[self.category]
        return cache.pages[index].items if index < len(cache.pages) else ()

    @property
    def total_selected(self) -> int:
        return sum(len(indices) for indices in self.selected.values())

    @property
    def rows(self) -> int:
        if self.category is None:
            return len(self.categories) + 1
        return len(self.items) + (1 if self.fixed_type else 2)

    @property
    def action_index(self) -> int:
        return self.rows - 1

    def move(self, step: int) -> None:
        if not self.loading:
            self.cursor = max(0, min(self.rows - 1, self.cursor + step))

    def open_category(self, kind: str) -> None:
        self.category = kind
        self.cursor = min(self.positions[kind], self.rows - 1)
        self.error = None

    def back(self) -> bool:
        """Go to the preceding page, then the category hub when available."""
        if self.loading or self.category is None:
            return False
        kind = self.category
        if self.pages[kind] > 0:
            self.pages[kind] -= 1
            self.cursor = 0
        elif not self.fixed_type:
            self.positions[kind] = self.cursor
            self.category = None
            self.cursor = self.root_cursor
        else:
            return False
        self.error = None
        return True

    def back_to_categories(self) -> None:
        """Leave the results view directly, retaining its cached page/cursor."""
        if self.category is None or self.fixed_type or self.loading:
            return
        self.positions[self.category] = self.cursor
        self.category = None
        self.cursor = self.root_cursor
        self.error = None

    def toggle(self) -> None:
        """Toggle one result only; never select a whole search category."""
        if self.loading or self.category is None or self.cursor >= len(self.items):
            return
        cache = self.caches[self.category]
        index = cache.offset(self.pages[self.category]) + self.cursor
        selected = self.selected[self.category]
        if index in selected:
            selected.remove(index)
        else:
            selected.add(index)
        self.error = None

    def result(self) -> dict[str, tuple[int, ...]]:
        """Return only explicitly selected result indices, by media type."""
        return {
            kind: tuple(sorted(self.selected[kind]))
            for kind, _ in self.categories
            if self.selected[kind]
        }


async def select_search_results(
    caches: Mapping[str, PageCache[dict]],
    term: str,
    *,
    media_type: str | None = None,
    input=None,
    output=None,
) -> dict[str, tuple[int, ...]]:
    """Browse types and pages in one full-screen session; return chosen results.

    ``media_type`` pins a single category for ``gamdl search --type``.
    Fetches run after opening the alternate screen; all errors, including the
    first-page 429, are recoverable without leaving the browser.
    """
    state = SearchBrowserState(caches, media_type=media_type)
    keys = KeyBindings()
    tasks: set[asyncio.Task] = set()

    def body() -> FormattedText:
        category = state.category
        if category is None:
            fragments = heading(f"Search · {term}", "Media types")
            for index, (kind, name) in enumerate(state.categories):
                number = len(state.selected[kind])
                label = f"{name}  ·  {number} selected" if number else name
                fragments.append(
                    (
                        "class:focused" if index == state.cursor else "",
                        f" {'❯' if index == state.cursor else ' '} {label}\n",
                    )
                )
        else:
            title = dict(SEARCH_CATEGORIES)[category]
            page = state.pages[category]
            fragments = heading(f"Search · {term}", f"{title} · Page {page + 1}")
            cache = state.caches[category]
            offset = cache.offset(page)
            for index, item in enumerate(state.items):
                attrs = item.get("attributes", {})
                name = attrs.get("name") or item.get("id", "Unknown")
                artist = attrs.get("artistName")
                label = f"{name} · {artist}" if artist else name
                mark = "[x]" if offset + index in state.selected[category] else "[ ]"
                fragments.append(
                    (
                        "class:focused" if index == state.cursor else "",
                        f" {'❯' if index == state.cursor else ' '} {mark} {label}\n",
                    )
                )
            if not state.items and not state.loading and not state.error:
                fragments.append(
                    ("class:muted", " No results found in this category\n")
                )
            if not state.fixed_type:
                index = len(state.items)
                fragments.append(
                    (
                        "class:focused" if state.cursor == index else "class:action",
                        f" {'❯' if state.cursor == index else ' '}     Back to media types\n",
                    )
                )

        index = state.action_index
        fragments.append(
            (
                "class:focused" if state.cursor == index else "class:action",
                f" {'❯' if state.cursor == index else ' '}     Download selected ({state.total_selected})\n",
            )
        )
        if state.loading:
            fragments.append(
                ("class:muted", " Loading search results... Backspace to cancel\n")
            )
        if state.error:
            fragments.append(("class:error", f" {state.error}\n"))
        return FormattedText(fragments)

    def controls() -> FormattedText:
        if state.category is None:
            instructions = (
                "↑/↓ Navigate · Enter/Space Browse · Enter Download · Esc Cancel"
            )
        else:
            instructions = (
                "↑/↓ Navigate · ←/→ Pages · Backspace/Esc Back · Space Select · Enter Select/Download"
                if not state.fixed_type
                else "↑/↓ Navigate · ←/→ Pages · Space Select · Enter Download · Esc Cancel"
            )
        return footer(
            f"{state.total_selected} selected across media types", instructions
        )

    def cursor_row() -> int:
        # The no-results placeholder occupies one additional line.
        empty = (
            state.category is not None
            and not state.items
            and not state.loading
            and not state.error
        )
        return state.cursor + 2 + int(empty)

    app = make_app(
        body=body,
        footer=controls,
        cursor_row=cursor_row,
        keys=keys,
        input=input,
        output=output,
    )

    def launch(coroutine) -> None:
        if state.loading:
            return
        state.loading = True
        state.error = None
        task = asyncio.create_task(coroutine)
        tasks.add(task)

        def done(finished):
            tasks.discard(finished)
            app.invalidate()

        task.add_done_callback(done)
        app.invalidate()

    def error_message(exc: Exception, *, next_page: bool = False) -> str:
        status = getattr(exc, "status_code", None)
        reason = f"HTTP {status}" if status is not None else type(exc).__name__
        action = "Press → to retry" if next_page else "Press Enter to retry"
        return f"Unable to load results ({reason}). {action}."

    async def load_first(kind: str) -> None:
        try:
            await caches[kind].get(0)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - network errors are recoverable
            state.error = error_message(exc)
        else:
            state.open_category(kind)
        finally:
            state.loading = False
            app.invalidate()

    async def advance_page(kind: str) -> None:
        try:
            next_index = state.pages[kind] + 1
            if await caches[kind].get(next_index) is not None:
                state.pages[kind] = next_index
                state.cursor = 0
                state.error = None
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - network errors are recoverable
            state.error = error_message(exc, next_page=True)
        finally:
            state.loading = False
            app.invalidate()

    def browse_category(kind: str) -> None:
        if state.loading:
            return
        if caches[kind].pages:
            state.open_category(kind)
        else:
            launch(load_first(kind))

    def confirm() -> None:
        if not state.total_selected:
            state.error = "Select individual search results before downloading"
            return
        app.exit(result=state.result())

    @keys.add("up")
    @keys.add("k")
    def up(event):
        state.move(-1)

    @keys.add("down")
    @keys.add("j")
    def down(event):
        state.move(1)

    @keys.add("home")
    def home(event):
        state.move(-state.rows)

    @keys.add("end")
    def end(event):
        state.move(state.rows)

    @keys.add("pageup")
    def page_up(event):
        state.move(-10)

    @keys.add("pagedown")
    def page_down(event):
        state.move(10)

    @keys.add("left")
    @keys.add("backspace")
    def back(event):
        if state.loading:
            for task in tuple(tasks):
                task.cancel()
            return
        if not state.back() and state.category is None:
            app.exit(result=None)

    @keys.add("right")
    def right(event):
        if state.loading or state.category is None:
            return
        kind = state.category
        if not caches[kind].pages:
            launch(load_first(kind))
        else:
            launch(advance_page(kind))

    @keys.add("space")
    def toggle(event):
        if state.loading:
            return
        if state.category is None:
            if state.cursor < len(state.categories):
                kind = state.categories[state.cursor][0]
                state.root_cursor = state.cursor
                browse_category(kind)
        else:
            state.toggle()

    @keys.add("c-a")
    def prevent_bulk_select(event):
        if not state.loading:
            state.error = "Select individual results; selecting an entire search category is disabled"

    @keys.add("enter")
    def enter(event):
        if state.loading:
            return
        if state.category is None:
            if state.cursor < len(state.categories):
                kind = state.categories[state.cursor][0]
                state.root_cursor = state.cursor
                browse_category(kind)
            else:
                confirm()
            return

        kind = state.category
        if not caches[kind].pages:
            launch(load_first(kind))
        elif state.cursor < len(state.items):
            state.toggle()
        elif not state.fixed_type and state.cursor == len(state.items):
            state.back_to_categories()
        else:
            confirm()

    @keys.add("escape")
    def escape(event):
        if state.loading:
            for task in tuple(tasks):
                task.cancel()
            if state.category is None or state.fixed_type:
                app.exit(result=None)
        elif not state.back():
            app.exit(result=None)

    @keys.add("c-c")
    def cancel(event):
        for task in tuple(tasks):
            task.cancel()
        app.exit(result=None)

    if media_type is not None:
        # Open the alternate screen before fetching the very first page.
        app.pre_run_callables.append(lambda: launch(load_first(media_type)))

    try:
        answer = await app.run_async()
    finally:
        for task in tuple(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    if answer is None:
        raise click.Abort()
    return answer
