"""Paginated full-screen catalog search result picker."""

from __future__ import annotations

import click
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings

from .app import make_app
from .components import footer, heading


async def select_search_results(cache, term: str):
    """Select occurrence indices; pages load on demand and selections persist."""
    first = await cache.get(0)
    if not first or not first.items:
        return []
    page = 0
    cursor = 0
    selected = set()
    error = None
    keys = KeyBindings()

    def items():
        return cache.pages[page].items

    def body():
        fragments = heading("GAMDL 4.0", f"Search · {term} · Page {page + 1}")
        start = cache.offset(page)
        for i, item in enumerate(items()):
            attrs = item.get("attributes", {})
            name = attrs.get("name", item.get("id", "?"))
            artist = attrs.get("artistName")
            title = f"{name} · {artist}" if artist else name
            mark = "[x]" if start + i in selected else "[ ]"
            fragments.append(
                (
                    "class:focused" if cursor == i else "",
                    f" {'❯' if cursor == i else ' '} {mark} {title}\n",
                )
            )
        fragments.append(
            (
                "class:focused" if cursor == len(items()) else "class:action",
                f" {'❯' if cursor == len(items()) else ' '}     Download selected\n",
            )
        )
        if error:
            fragments.append(("class:error", f" {error}\n"))
        return FormattedText(fragments)

    app = make_app(
        body=body,
        footer=lambda: footer(
            f"{len(selected)} selected · Page {page + 1}",
            "↑/↓ Navigate · ←/→ Pages · Space Toggle · Ctrl+A All · Enter Confirm · Esc Cancel",
        ),
        cursor_row=lambda: cursor + 2,
        keys=keys,
    )

    @keys.add("up")
    def up(event):
        nonlocal cursor
        cursor = max(0, cursor - 1)

    @keys.add("down")
    def down(event):
        nonlocal cursor
        cursor = min(len(items()), cursor + 1)

    @keys.add("left")
    def left(event):
        nonlocal page, cursor
        if page:
            page -= 1
            cursor = 0

    @keys.add("right")
    def right(event):
        async def advance():
            nonlocal page, cursor
            if await cache.get(page + 1) is not None:
                page += 1
                cursor = 0
            app.invalidate()

        event.app.create_background_task(advance())

    @keys.add("space")
    def toggle(event):
        nonlocal error
        if cursor < len(items()):
            index = cache.offset(page) + cursor
            if index in selected:
                selected.remove(index)
            else:
                selected.add(index)
            error = None

    @keys.add("c-a")
    def all_items(event):
        async def load():
            nonlocal error
            try:
                async for index, _ in cache.iter_items():
                    selected.add(index)
            except Exception as exc:
                error = str(exc)
            app.invalidate()

        event.app.create_background_task(load())

    @keys.add("enter")
    def confirm(event):
        nonlocal error
        if cursor < len(items()):
            toggle(event)
        elif selected:
            app.exit(result=sorted(selected))
        else:
            error = "Select at least one result"

    @keys.add("escape")
    @keys.add("c-c")
    def cancel(event):
        app.exit(result=None)

    answer = await app.run_async()
    if answer is None:
        raise click.Abort()
    return answer
