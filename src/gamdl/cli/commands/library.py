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
    """Browse or automatically download your Apple Music library.

    Browse playlists, albums, songs and music videos interactively.
    """
    if options.select or not options.library_auto_select:
        require_terminal("Library browsing")
    from .runner import run_rip

    try:
        rip_options = RipOptions(
            **{
                k: v
                for k, v in asdict(options).items()
                if k not in {"library_auto_select", "select"}
            },
            urls=("https://music.apple.com/library",),
            select=options.select,
        )
        rip_options.library_auto_select = options.library_auto_select
        code = asyncio.run(run_rip(rip_options))
    except KeyboardInterrupt:
        raise click.Abort() from None
    if code:
        raise SystemExit(code)
