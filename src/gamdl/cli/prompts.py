"""Typed prompt routing: full-screen menus, simple secure credential inputs."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from gamdl.media.models import (
    ArtistBrowseRequest,
    ArtistBrowseResult,
    CollectionSelectionRequest,
    SelectionRequest,
)

from .tui.browser import browse_artist
from .tui.components import MenuChoice
from .tui.multiselect import select_many
from .tui.selector import select_stream as _select_stream
from .tui.selector import stream_choices  # noqa: F401 - public re-export
from .tui.wizard import FullScreenWizard


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def require_terminal(description: str) -> None:
    if not interactive():
        raise click.ClickException(f"{description} requires an interactive terminal")


async def select_stream(request, *, media_title: str | None = None) -> int:
    """Choose a source, codec, video/audio rendition or uploaded-video quality."""
    require_terminal("Stream selection")
    return await _select_stream(request, title=media_title)


async def select_media(
    request: SelectionRequest | CollectionSelectionRequest | ArtistBrowseRequest,
) -> list[int] | ArtistBrowseResult:
    """Browse an artist or choose album/playlist track occurrences."""
    require_terminal("Media selection")
    if isinstance(request, ArtistBrowseRequest):
        return await browse_artist(request)
    title = (
        request.prompt
        if isinstance(request, CollectionSelectionRequest)
        else f"Select {request.kind.value.replace('_', ' ')} to download"
    )
    return await select_many(
        title, [MenuChoice(item.title, item.index) for item in request.options]
    )


async def ask_text(message: str, *, password: bool = False, default: str = "") -> str:
    """Use the same alternate-screen input as other interactive workflows."""
    require_terminal(message)
    wizard = FullScreenWizard()

    async def flow(ui: FullScreenWizard) -> str:
        return await ui.ask(
            message.removesuffix(":"),
            message.removesuffix(":"),
            default=default,
            password=password,
        )

    return await wizard.run(flow)


async def ask_file(
    path: str | None, *, label: str, extension: str | None = None
) -> Path:
    candidate = Path(path).expanduser() if path else None
    while (
        candidate is None
        or not candidate.is_file()
        or (extension and candidate.suffix.lower() != extension)
    ):
        if not interactive():
            raise click.ClickException(
                f"{label} file missing or invalid; supply a valid path via "
                "--cookies-path/--device-path or YAML configuration"
            )
        suggestion = str(candidate) if candidate else ""
        answer = await ask_text(
            f"{label} file missing. Enter a path or drag the file here:",
            default=suggestion,
        )
        value = answer.strip().strip("\"'")
        candidate = Path(value).expanduser() if value else None
    return candidate
