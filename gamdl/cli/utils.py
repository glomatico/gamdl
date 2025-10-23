import asyncio
import logging
import typing
from functools import wraps
from pathlib import Path

import click

from .config_file import ConfigFile


class Csv(click.ParamType):
    name = "csv"

    def __init__(
        self,
        subtype: typing.Any,
    ) -> None:
        self.subtype = subtype

    def convert(
        self,
        value: str | typing.Any,
        param: click.Parameter,
        ctx: click.Context,
    ) -> list[typing.Any]:
        if not isinstance(value, str):
            return value

        items = [v.strip() for v in value.split(",") if v.strip()]
        result = []

        for item in items:
            try:
                result.append(self.subtype(item))
            except ValueError as e:
                self.fail(
                    f"'{item}' is not a valid value for {self.subtype.__name__}",
                    param,
                    ctx,
                )
        return result


class PathPrompt(click.ParamType):
    name = "path"

    def __init__(self, is_file: bool = False) -> None:
        self.is_file = is_file

    def convert(
        self,
        value: str | typing.Any,
        param: click.Parameter,
        ctx: click.Context,
    ) -> str:
        if not isinstance(value, str):
            return value

        path_validator = click.Path(
            exists=True,
            file_okay=self.is_file,
            dir_okay=not self.is_file,
        )
        path_type = "file" if self.is_file else "directory"
        while True:
            try:
                result = path_validator.convert(value, None, None)
                break
            except click.BadParameter as e:
                value = click.prompt(
                    (
                        f'{path_type.capitalize()} "{Path(value).absolute()}" does not exist. '
                        f"Create the {path_type} at the specified path, "
                        f"type a new path or drag and drop the {path_type} here. "
                        "Then, press enter to continue"
                    ),
                    default=value,
                    show_default=False,
                )
                value = value.strip('"')
        return result


class CustomLoggerFormatter(logging.Formatter):
    base_format = "[%(levelname)-8s %(asctime)s]"
    format_colors = {
        logging.DEBUG: dict(dim=True),
        logging.INFO: dict(fg="green"),
        logging.WARNING: dict(fg="yellow"),
        logging.ERROR: dict(fg="red"),
        logging.CRITICAL: dict(fg="red", bold=True),
    }
    date_format = "%H:%M:%S"

    def __init__(self, use_colors: bool = True) -> None:
        super().__init__()
        self.use_colors = use_colors

    def format(self, record: logging.LogRecord) -> str:
        return logging.Formatter(
            (
                click.style(self.base_format, **self.format_colors.get(record.levelno))
                if self.use_colors
                else self.base_format
            )
            + " %(message)s",
            datefmt=self.date_format,
        ).format(record)


def load_config_file(
    ctx: click.Context,
    param: click.Parameter,
    no_config_file: bool,
) -> click.Context:
    if no_config_file:
        return ctx

    config_file = ConfigFile(ctx.params["config_path"])
    config_file.add_params_default_to_config(
        ctx.command.params,
    )
    parsed_params = config_file.parse_params_from_config(
        [
            param
            for param in ctx.command.params
            if ctx.get_parameter_source(param.name)
            != click.core.ParameterSource.COMMANDLINE
        ]
    )
    ctx.params.update(parsed_params)

    return ctx


def make_sync(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return wrapper
