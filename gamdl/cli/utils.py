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


def prompt_path(
    input_path: str,
    is_dir: bool = False,
) -> str:
    path_validator = click.Path(
        exists=True,
        file_okay=not is_dir,
        dir_okay=is_dir,
    )
    path_type = "directory" if is_dir else "file"

    while True:
        try:
            result_path = path_validator.convert(input_path, None, None)
            break
        except click.BadParameter as e:
            input_path = click.prompt(
                (
                    f'{path_type.capitalize()} "{Path(input_path).absolute()}" does not exist. '
                    f"Create the {path_type} at the specified path, "
                    f"type a new path or drag and drop the {path_type} here. "
                    "Then, press enter to continue"
                ),
                default=input_path,
                show_default=False,
            )
            input_path = input_path.strip('"')

    return result_path
