"""Presentation-only primitives shared by all interactive menus."""

from dataclasses import dataclass

from prompt_toolkit.formatted_text import FormattedText


@dataclass(frozen=True)
class MenuChoice:
    title: str
    value: int
    detail: str = ""


def heading(title: str, subtitle: str | None = None) -> list[tuple[str, str]]:
    line = title if not subtitle else f"{title} / {subtitle}"
    return [("class:heading", f"{line}\n"), ("class:muted", "─" * 64 + "\n")]


def footer(summary: str, controls: str) -> FormattedText:
    return FormattedText(
        [
            ("class:muted", f" {summary}\n"),
            ("class:muted", f" {controls}"),
        ]
    )
