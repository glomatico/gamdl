"""Full-screen multi-select list for album and playlist track occurrences."""

from __future__ import annotations

import click
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings

from .app import make_app
from .components import MenuChoice, footer, heading


class MultiChoiceState:
    def __init__(self, choices: list[MenuChoice]):
        self.choices = choices
        self.cursor = 0
        self.selected: set[int] = set()
        self.error: str | None = None

    @property
    def rows(self):
        return len(self.choices) + 1

    def move(self, step: int):
        self.cursor = max(0, min(self.rows - 1, self.cursor + step))

    def toggle(self):
        if self.cursor >= len(self.choices):
            return
        index = self.choices[self.cursor].value
        if index in self.selected:
            self.selected.remove(index)
        else:
            self.selected.add(index)
        self.error = None

    def select_all(self):
        self.selected = {item.value for item in self.choices}
        self.error = None

    def clear(self):
        self.selected.clear()
        self.error = None

    def invert(self):
        self.selected = {item.value for item in self.choices} - self.selected
        self.error = None

    def result(self):
        # Preserve the original source occurrence order, not toggle order.
        return [item.value for item in self.choices if item.value in self.selected]


async def select_many(
    title: str, choices: list[MenuChoice], *, input=None, output=None
) -> list[int]:
    if not choices:
        return []
    state = MultiChoiceState(choices)
    keys = KeyBindings()

    def body() -> FormattedText:
        fragments = heading("GAMDL 4.0", title)
        for i, item in enumerate(choices):
            mark = "[x]" if item.value in state.selected else "[ ]"
            fragments.append(
                (
                    "class:focused" if i == state.cursor else "",
                    f" {'❯' if i == state.cursor else ' '} {mark}  {item.title}\n",
                )
            )
        fragments.append(
            (
                "class:focused" if state.cursor == len(choices) else "class:action",
                f" {'❯' if state.cursor == len(choices) else ' '}      Download selected\n",
            )
        )
        if state.error:
            fragments.append(("class:error", f"\n {state.error}"))
        return FormattedText(fragments)

    app = make_app(
        body=body,
        footer=lambda: footer(
            f"{len(state.selected)} of {len(choices)} selected",
            "↑/↓ Navigate  ·  PgUp/PgDn Jump  ·  Space Toggle  ·  Ctrl+A All  ·  Ctrl+I Invert  ·  Ctrl+D Clear  ·  Enter Confirm  ·  Esc Cancel",
        ),
        cursor_row=lambda: state.cursor + 2,
        keys=keys,
        input=input,
        output=output,
    )

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
        state.cursor = 0

    @keys.add("end")
    def end(event):
        state.cursor = state.rows - 1

    @keys.add("pageup")
    def pageup(event):
        state.move(-10)

    @keys.add("pagedown")
    def pagedown(event):
        state.move(10)

    @keys.add("space")
    def toggle(event):
        state.toggle()

    @keys.add("c-a")
    def all_items(event):
        state.select_all()

    @keys.add("c-i")
    def invert(event):
        state.invert()

    @keys.add("c-d")
    def clear(event):
        state.clear()

    @keys.add("enter")
    def confirm(event):
        if state.cursor < len(choices):
            state.toggle()
        elif state.selected:
            app.exit(result=state.result())
        else:
            state.error = "Select at least one item before downloading"

    @keys.add("escape")
    @keys.add("c-c")
    def cancel(event):
        app.exit(result=None)

    answer = await app.run_async()
    if answer is None:
        raise click.Abort()
    return answer
