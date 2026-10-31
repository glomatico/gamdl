"""Search the Apple Music catalog and download selected results."""

import asyncio
from dataclasses import asdict

import click
from dataclass_click import dataclass_click

from gamdl.cli.options import RipOptions, SearchOptions
from gamdl.cli.prompts import require_terminal


@click.command("search")
@dataclass_click(SearchOptions)
def search(options: SearchOptions) -> None:
    """Search one term and download catalog songs or videos."""
    if not options.term.strip():
        raise click.UsageError("Provide a nonempty search term")
    if options.feeling_lucky and not options.media_type:
        raise click.UsageError("--feeling-lucky requires --type")
    if options.search_api == "itunes" and options.media_type in {
        "playlist",
        "user-playlist",
    }:
        raise click.UsageError("iTunes search does not support playlists")
    if not options.feeling_lucky:
        require_terminal("Search selection")
    from .runner import run_rip

    rip_options = RipOptions(
        **{
            k: v
            for k, v in asdict(options).items()
            if k not in {"term", "media_type", "feeling_lucky", "search_api"}
        },
        urls=("search",),
    )
    rip_options.search_term = options.term
    rip_options.search_api = options.search_api
    rip_options.search_type = options.media_type
    rip_options.feeling_lucky = options.feeling_lucky
    try:
        code = asyncio.run(run_rip(rip_options))
    except KeyboardInterrupt:
        raise click.Abort() from None
    if code:
        raise SystemExit(code)
