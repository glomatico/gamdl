"""Click entrypoint; implementation and option schema live in separate modules."""

import asyncio

import click
from dataclass_click import dataclass_click

from gamdl.cli.options import RipOptions
from .input_urls import expand_rip_inputs


@click.command("rip")
@dataclass_click(RipOptions)
def rip(options: RipOptions) -> None:
    """Download Apple Music URLs, ISRCs, or URL lists from local files."""
    from .runner import run_rip

    options.urls = expand_rip_inputs(options.urls)
    try:
        exit_code = asyncio.run(run_rip(options))
    except KeyboardInterrupt:
        raise click.Abort() from None
    if exit_code:
        raise SystemExit(exit_code)
