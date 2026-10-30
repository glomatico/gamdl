"""GAMDL command group; YAML is loaded before subcommand option parsing."""

from pathlib import Path

import click

from gamdl import __version__

from .config.commands import config_group
from .config.loader import ConfigError, click_default_map, load_config
from .config.models import config_path as default_config_path
from .errors import CliCommand


@click.group(cls=CliCommand, context_settings={"auto_envvar_prefix": "GAMDL"})
@click.version_option(__version__, "--version", "-v")
@click.option(
    "--config",
    "config_file",
    type=click.Path(path_type=Path, dir_okay=False),
    envvar="GAMDL_CONFIG",
    help="YAML configuration path",
)
@click.option("--no-config", is_flag=True, help="Ignore YAML configuration")
@click.pass_context
def main(ctx: click.Context, config_file: Path | None, no_config: bool) -> None:
    """GAMDL 4.0 — download Apple Music with configurable codecs and progress."""
    path = (config_file or default_config_path()).expanduser()
    ctx.obj = {
        "config_path": path,
        "config_explicit": config_file is not None,
        "no_config": no_config,
    }
    try:
        config = load_config(None if no_config else path)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from None
    ctx.obj["settings"] = config
    defaults = click_default_map(config)
    ctx.default_map = {"rip": defaults, "library": defaults, "search": defaults}


from .commands.auth import auth
from .commands.database import database_group
from .commands.library import library
from .commands.rip import rip
from .commands.search import search

main.add_command(rip)
main.add_command(auth)
main.add_command(library)
main.add_command(search)
main.add_command(config_group)
main.add_command(database_group)
