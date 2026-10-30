"""Explicit config lifecycle commands; ordinary downloads are read-only."""

from dataclasses import asdict
from pathlib import Path

import click
import yaml

from .loader import load_config
from .models import AppConfig


@click.group(name="config")
def config_group() -> None:
    """Initialize, inspect or validate GAMDL YAML settings."""


@config_group.command("path")
@click.pass_context
def show_path(ctx: click.Context) -> None:
    """Print the effective YAML configuration path."""
    click.echo(ctx.find_root().obj["config_path"])


@config_group.command("init")
@click.option("--force", is_flag=True, help="Replace an existing YAML config")
@click.pass_context
def initialize(ctx: click.Context, force: bool) -> None:
    """Write a commented, versioned YAML config explicitly."""
    path: Path = ctx.find_root().obj["config_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    template = (
        "# GAMDL 4.0 configuration; CLI flags override these values.\n"
        "# Configuration schema changes are normalized automatically.\n"
        "\n" + yaml.safe_dump(asdict(AppConfig()), sort_keys=False, allow_unicode=True)
    )
    try:
        # Exclusive creation by default; --force intentionally opts into replace.
        with path.open("w" if force else "x", encoding="utf-8") as output:
            output.write(template)
    except FileExistsError:
        raise click.ClickException(
            f"Config already exists: {path} (use --force)"
        ) from None
    except OSError:
        raise click.ClickException("Unable to write config") from None
    click.echo(f"Created {path}")


@config_group.command("validate")
@click.pass_context
def validate(ctx: click.Context) -> None:
    """Validate YAML options without modifying the file."""
    root = ctx.find_root().obj
    if (
        root["config_explicit"]
        and not root["no_config"]
        and not root["config_path"].is_file()
    ):
        raise click.ClickException("Specified YAML configuration does not exist")
    config = load_config(None if root["no_config"] else root["config_path"])
    if not root["no_config"] and not root["config_path"].is_file():
        click.echo(
            "No YAML config found; built-in defaults are valid (use 'gamdl config init')."
        )
    else:
        click.echo(
            f"Valid config_version={config.config_version}: {root['config_path']}"
        )


@config_group.command("show")
@click.pass_context
def show(ctx: click.Context) -> None:
    """Show the effective YAML config (never includes authentication tokens)."""
    root = ctx.find_root().obj
    config = load_config(None if root["no_config"] else root["config_path"])
    shown = asdict(config)
    if shown["auth"]["media_user_token"] is not None:
        shown["auth"]["media_user_token"] = "[REDACTED]"
    click.echo(yaml.safe_dump(shown, sort_keys=False, allow_unicode=True))
