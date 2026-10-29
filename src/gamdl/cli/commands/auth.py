"""Explicitly configure or change the Apple Music authentication method."""

import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace

import click

from gamdl.api import GamdlApiError

from ..output import console
from ..session import authenticate


@click.command("auth")
@click.pass_context
def auth(ctx: click.Context) -> None:
    """Choose, validate, and save an Apple Music authentication method."""
    root = ctx.find_root().obj
    if root["no_config"]:
        raise click.ClickException("'gamdl auth' requires YAML configuration")
    config = root["settings"].auth
    options = SimpleNamespace(
        **{**vars(config), "auth_mode": config.mode},
    )

    async def run() -> None:
        async with AsyncExitStack() as stack:
            await authenticate(options, stack, console=console(), force_setup=True)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        raise click.Abort() from None
    except GamdlApiError:
        raise click.ClickException(
            "Apple Music authentication could not be validated; settings were not saved"
        ) from None
