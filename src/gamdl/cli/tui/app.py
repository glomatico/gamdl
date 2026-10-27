"""Full-screen PTK application factory with a common scrollable layout."""

from collections.abc import Callable
from typing import Any

from prompt_toolkit import Application
from prompt_toolkit.formatted_text import AnyFormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.screen import Point

from .theme import THEME


def make_app(
    *,
    body: Callable[[], AnyFormattedText],
    footer: Callable[[], AnyFormattedText],
    cursor_row: Callable[[], int],
    keys: KeyBindings,
    input: Any = None,
    output: Any = None,
) -> Application:
    """Keep choices scrollable and terminal history intact via alt-screen."""
    content = Window(
        content=FormattedTextControl(
            body,
            focusable=True,
            get_cursor_position=lambda: Point(0, cursor_row()),
        ),
        wrap_lines=False,
        always_hide_cursor=True,
    )
    bottom = Window(content=FormattedTextControl(footer), height=2)
    return Application(
        layout=Layout(HSplit([content, bottom]), focused_element=content),
        key_bindings=keys,
        full_screen=True,
        style=THEME,
        **({"input": input} if input is not None else {}),
        **({"output": output} if output is not None else {}),
    )
