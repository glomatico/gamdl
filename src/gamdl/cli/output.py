"""Unified Rich-based human output; do not display request URLs or secrets."""

from rich.console import Console
from rich.text import Text

from gamdl import __version__


def console(*, stderr: bool = False) -> Console:
    return Console(stderr=stderr, markup=True, highlight=False)


def banner(output: Console | None = None) -> None:
    # Human-facing decoration must not contaminate piped command results.
    output = output if output is not None else console(stderr=True)
    major_minor = ".".join(__version__.split(".")[:2])
    output.print(Text(f"GAMDL {major_minor}", style="bold cyan"))
    output.print()


def message(content: str, *, symbol: str, style: str, stderr: bool = False) -> None:
    text = Text()
    text.append(symbol, style=style)
    text.append(content)
    console(stderr=stderr).print(text)


def info(content: str) -> None:
    message(content, symbol="• ", style="cyan")


def success(content: str) -> None:
    message(content, symbol="✓ ", style="green")


def warning(content: str) -> None:
    message(content, symbol="! ", style="yellow", stderr=True)


def error(content: str) -> None:
    message(content, symbol="✗ ", style="red", stderr=True)
