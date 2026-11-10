"""Synchronize local library state with Apple Music."""

import click

from .playlists import playlists


@click.group("sync", invoke_without_command=True)
@click.pass_context
def sync(ctx: click.Context) -> None:
    """Reconcile local library content with its Apple Music sources."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


sync.add_command(playlists)
