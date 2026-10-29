"""Browse and download selected items from the authenticated Apple Music library."""

import asyncio
from dataclasses import asdict

import click
from dataclass_click import dataclass_click

from gamdl.cli.options import LibraryOptions, RipOptions
from gamdl.cli.prompts import require_terminal


@click.command("library")
@dataclass_click(LibraryOptions)
def library(options: LibraryOptions) -> None:
    """Interactively browse and download your Apple Music library.

    Browse playlists, albums, songs and music videos interactively.
    """
    require_terminal("Library browsing")
    from .runner import run_rip

    try:
        code = asyncio.run(
            run_rip(
                RipOptions(
                    **asdict(options),
                    urls=("https://music.apple.com/library",),
                    select=True,
                )
            )
        )
    except KeyboardInterrupt:
        raise click.Abort() from None
    if code:
        raise SystemExit(code)
