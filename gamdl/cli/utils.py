import atexit
import sys
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

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


class CustomOutputWriter:
    def __init__(
        self,
        streams: list[Any] = [sys.stdout],
    ):
        self.streams = streams

    def add_file(self, path: str):
        file_stream = open(path, "a")
        atexit.register(file_stream.close)
        self.streams.append(file_stream)

    def write(self, message: str):
        for stream in self.streams:
            stream.write(message)

    def flush(self):
        for stream in self.streams:
            stream.flush()


def custom_structlog_formatter(
    logger: Any,
    name: str,
    event_dict: dict[str, Any],
) -> str:
    level = event_dict.get("level", "INFO").upper()
    timestamp = datetime.now().strftime("%H:%M:%S")

    level_colors = {
        "DEBUG": "cyan",
        "INFO": "green",
        "WARNING": "yellow",
        "ERROR": "red",
        "CRITICAL": "red",
    }

    color = level_colors.get(level, "white")
    prefix = click.style(f"[{level:<8} {timestamp}]", fg=color)

    action = event_dict.pop("action", None)
    if action:
        prefix += click.style(f" [{action}]", dim=True)

    if level in {"INFO", "WARNING", "ERROR", "CRITICAL"}:
        message = event_dict.get("event", "")
        return f"{prefix} {message}"
    else:
        return f"{prefix} {event_dict}"


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
