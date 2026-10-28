"""Browse and download selected items from the authenticated Apple Music library."""

import asyncio
from dataclasses import replace

import click
from dataclass_click import dataclass_click

from gamdl.cli.options import RipOptions
from gamdl.cli.prompts import require_terminal


@click.command("library")
@dataclass_click(RipOptions)
def library(options: RipOptions) -> None:
    """Interactively browse and download your Apple Music library.

    Browse playlists, albums, songs and music videos with the same options
    as `gamdl rip`, without having to provide an Apple Music URL.
    """
    require_terminal("Library browsing")
    from .runner import run_rip

    try:
        code = asyncio.run(
            run_rip(replace(options, urls=("https://music.apple.com/library",)))
        )
    except KeyboardInterrupt:
        raise click.Abort() from None
    if code:
        raise SystemExit(code)


# The library source is implicit. Reuse the download/codec/auth options from
# rip, but do not expose its positional URLs argument in this command.
library.params = [param for param in library.params if param.name != "urls"]
