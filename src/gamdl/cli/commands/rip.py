"""Click entrypoint; implementation and option schema live in separate modules."""

import asyncio

import click
from dataclass_click import dataclass_click

from gamdl.cli.options import RipOptions


@click.command("rip")
@dataclass_click(RipOptions)
def rip(options: RipOptions) -> None:
    """Download one or more Apple Music URLs in a shared session."""
    from .runner import run_rip

    try:
        exit_code = asyncio.run(run_rip(options))
    except KeyboardInterrupt:
        raise click.Abort() from None
    if exit_code:
        raise SystemExit(exit_code)
