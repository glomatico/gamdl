import logging
from enum import Enum
from pathlib import Path

import click


class Csv(click.ParamType):
    name = "csv"

    def __init__(
        self,
        subtype: Enum,
    ) -> None:
        self.subtype = subtype

    def convert(
        self,
        value: str,
        param: click.Parameter,
        ctx: click.Context,
    ) -> list[Enum]:
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
        value: str,
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
