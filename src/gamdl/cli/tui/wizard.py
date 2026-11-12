"""Reusable multi-step full-screen wizard with inline, optionally masked input.

Unlike a sequence of separate prompts, the alternate screen stays open across
choices, text entry, asynchronous work, errors and confirmation screens.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import click
from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import ConditionalProcessor, PasswordProcessor
from prompt_toolkit.layout.screen import Point

from .components import MenuChoice, footer, heading
from .theme import THEME

T = TypeVar("T")


class WizardBack(Exception):
    """Return to the previous step without tearing down the terminal UI."""


class FullScreenWizard:
    """One prompt_toolkit Application for an entire interactive workflow.

    The flow can await `choose` and `ask` repeatedly, interleave HTTP requests,
    and accept asynchronous callbacks that request additional credentials.
    """

    def __init__(
        self,
        *,
        summary: str = "Interactive setup",
        input: Any = None,
        output: Any = None,
    ) -> None:
        self.summary = summary
        self.title = ""
        self.description = ""
        self.stage = "busy"
        self.choices: list[MenuChoice] = []
        self.cursor = 0
        self.label = ""
        self.secret = False
        self._pending: asyncio.Future[Any] | None = None
        self.buffer = Buffer(multiline=False)
        keys = KeyBindings()

        def body_cursor_position() -> Point:
            # The cursor must always refer to a rendered line. In particular,
            # choosing the last authentication method leaves `self.cursor` at
            # 2, but the next entry/busy screen has only one content row.
            return Point(
                0,
                3
                + (1 if self.description else 0)
                + (self.cursor if self.stage == "choice" else 0),
            )

        def body() -> FormattedText:
            fragments = heading(self.title)
            if self.description:
                fragments.append(("class:detail", f" {self.description}\n"))
            fragments.append(("", "\n"))
            if self.stage == "choice":
                for index, choice in enumerate(self.choices):
                    label = choice.title
                    if choice.detail:
                        label += f"  ·  {choice.detail}"
                    focused = index == self.cursor
                    fragments.append(
                        (
                            "class:focused" if focused else "",
                            f" {'❯' if focused else ' '} {label}\n",
                        )
                    )
            elif self.stage == "entry":
                fragments.append(("class:detail", f" {self.label}\n"))
            elif self.stage == "busy":
                fragments.append(("class:muted", " Working...\n"))
            return FormattedText(fragments)

        self.body_window = Window(
            FormattedTextControl(
                body,
                focusable=True,
                get_cursor_position=body_cursor_position,
            ),
            height=Dimension(min=5, preferred=12, max=18),
            always_hide_cursor=True,
            wrap_lines=False,
        )
        self.input_window = Window(
            BufferControl(
                buffer=self.buffer,
                input_processors=[
                    ConditionalProcessor(
                        PasswordProcessor(char="•"),
                        filter=Condition(lambda: self.secret),
                    )
                ],
            ),
            height=1,
            wrap_lines=False,
        )
        self.footer_window = Window(
            FormattedTextControl(
                lambda: footer(
                    self.summary,
                    (
                        "↑/↓ Navigate · Enter Select · Backspace/Esc Back · Ctrl+C Cancel"
                        if self.stage == "choice"
                        else "Enter Continue · Ctrl+U Clear · Esc Back · Ctrl+C Cancel"
                        if self.stage == "entry"
                        else "Esc Cancel · Ctrl+C Cancel"
                    ),
                )
            ),
            height=2,
        )
        self.app: Application = Application(
            layout=Layout(
                HSplit(
                    [
                        self.body_window,
                        ConditionalContainer(
                            self.input_window,
                            filter=Condition(lambda: self.stage == "entry"),
                        ),
                        Window(),
                        self.footer_window,
                    ]
                ),
                focused_element=self.body_window,
            ),
            key_bindings=keys,
            full_screen=True,
            style=THEME,
            **({"input": input} if input is not None else {}),
            **({"output": output} if output is not None else {}),
        )
        # Treat a lone Escape promptly while still accepting arrow sequences.
        self.app.ttimeoutlen = 0.1
        self.app.timeoutlen = 0.25

        @keys.add("up")
        @keys.add("k", filter=Condition(lambda: self.stage == "choice"))
        def up(event):
            if self.stage == "choice":
                self.cursor = max(0, self.cursor - 1)

        @keys.add("down")
        @keys.add("j", filter=Condition(lambda: self.stage == "choice"))
        def down(event):
            if self.stage == "choice":
                self.cursor = min(len(self.choices) - 1, self.cursor + 1)

        @keys.add("enter")
        def enter(event):
            if self.stage == "choice":
                self._resolve(self.choices[self.cursor].value)
            elif self.stage == "entry":
                self._resolve(self.buffer.text)

        @keys.add(
            "backspace",
            filter=Condition(
                lambda: (
                    self.stage == "choice"
                    or (self.stage == "entry" and not self.buffer.text)
                )
            ),
        )
        @keys.add("escape")
        def back(event):
            if self.stage in {"choice", "entry"}:
                self._resolve(WizardBack())
            else:
                self.app.exit(result=None)

        @keys.add("c-c")
        def cancel(event):
            self.app.exit(result=None)

    def _resolve(self, result: Any) -> None:
        pending = self._pending
        if pending is None or pending.done():
            return
        self._pending = None
        if self.stage == "entry":
            self.buffer.text = ""
            self.secret = False
        self.stage = "busy"
        self.app.layout.focus(self.body_window)
        pending.set_result(result)
        self.app.invalidate()

    async def _step(self) -> Any:
        self._pending = asyncio.get_running_loop().create_future()
        self.app.invalidate()
        result = await self._pending
        if isinstance(result, WizardBack):
            raise result
        return result

    async def choose(
        self, title: str, choices: list[MenuChoice], *, description: str = ""
    ) -> int:
        if not choices:
            raise ValueError("Wizard choice list must not be empty")
        self.stage = "choice"
        self.title = title
        self.description = description
        self.choices = choices
        self.cursor = 0
        self.app.layout.focus(self.body_window)
        return await self._step()

    async def ask(
        self,
        title: str,
        label: str,
        *,
        description: str = "",
        default: str = "",
        password: bool = False,
    ) -> str:
        self.title = title
        self.description = description
        self.label = label
        self.secret = password
        self.buffer.text = default
        self.buffer.cursor_position = len(default)
        self.stage = "entry"
        self.app.layout.focus(self.input_window)
        return await self._step()

    def status(self, title: str, description: str) -> None:
        self.stage = "busy"
        self.title = title
        self.description = description
        self.app.layout.focus(self.body_window)
        self.app.invalidate()

    async def run(self, flow: Callable[[FullScreenWizard], Awaitable[T]]) -> T:
        async def runner() -> None:
            try:
                result = await flow(self)
            except (click.Abort, WizardBack):
                self.app.exit(result=None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - propagate failures via PTK
                self.app.exit(exception=exc)
            else:
                self.app.exit(result=result)

        task = asyncio.create_task(runner())
        try:
            answer = await self.app.run_async()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if answer is None:
            raise click.Abort()
        return answer
