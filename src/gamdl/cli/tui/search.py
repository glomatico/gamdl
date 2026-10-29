"""Paginated full-screen catalog search result picker."""

from __future__ import annotations

import asyncio

import click
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings

from .app import make_app
from .components import footer, heading


async def select_search_results(cache, term: str, *, input=None, output=None):
    """Select occurrence indices; pages load on demand and selections persist."""
    first = await cache.get(0)
    if not first or not first.items:
        return []
    page = 0
    cursor = 0
    selected = set()
    error = None
    loading = False
    tasks: set[asyncio.Task] = set()
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
        if loading:
            fragments.append(
                ("class:muted", " Loading results... Backspace to cancel\n")
            )
        return FormattedText(fragments)

    app = make_app(
        body=body,
        footer=lambda: footer(
            f"{len(selected)} selected · Page {page + 1}",
            "↑/↓ Navigate · ←/→ Pages · Backspace Previous · Space Toggle · Ctrl+A All · Enter Confirm · Esc Cancel",
        ),
        cursor_row=lambda: cursor + 2,
        keys=keys,
        input=input,
        output=output,
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
    @keys.add("backspace")
    def left(event):
        nonlocal page, cursor
        if loading:
            for task in tuple(tasks):
                task.cancel()
            return
        if page:
            page -= 1
            cursor = 0
        else:
            app.exit(result="back")

    def start_task(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.add(task)
        task.add_done_callback(lambda done: (tasks.discard(done), app.invalidate()))

    @keys.add("right")
    def right(event):
        if loading:
            return

        async def advance():
            nonlocal page, cursor, error, loading
            loading = True
            error = None
            app.invalidate()
            try:
                if await cache.get(page + 1) is not None:
                    page += 1
                    cursor = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                error = f"Unable to load page ({'HTTP ' + str(status) if status else type(exc).__name__}). Press → to retry."
            finally:
                loading = False
                app.invalidate()

        start_task(advance())

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
        if loading:
            return

        async def load():
            nonlocal error, loading
            loading = True
            error = None
            app.invalidate()
            try:
                async for index, _ in cache.iter_items():
                    selected.add(index)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                error = f"Unable to load results ({'HTTP ' + str(status) if status else type(exc).__name__})."
            finally:
                loading = False
                app.invalidate()

        start_task(load())

    @keys.add("enter")
    def confirm(event):
        nonlocal error
        if loading:
            return
        if cursor < len(items()):
            toggle(event)
        elif selected:
            app.exit(result=sorted(selected))
        else:
            error = "Select at least one result"

    @keys.add("escape")
    @keys.add("c-c")
    def cancel(event):
        for task in tuple(tasks):
            task.cancel()
        app.exit(result=None)

    try:
        answer = await app.run_async()
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    if answer is None:
        raise click.Abort()
    return answer
