import atexit
import os
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
        file_stream = open(path, "a", encoding="utf-8")
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
    level = event_dict.pop("level", "INFO").upper()
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
        message = event_dict.pop("event", "")
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

def get_default_config_path() -> str:
    platform = sys.platform

    # win32, but shouldn't have a conflict + future compat perhaps
    if platform.startswith("win"):
        return str(Path.home() / ".gamdl" / "config.ini")

    if platform.startswith("linux"):
        if cfg_dir := os.environ.get("XDG_CONFIG_HOME"):
            return str(Path(cfg_dir) / "gamdl" / "config.ini")

        # Path.home() calls expanduser which already checks $HOME, so it
        # should get the preferred home directory
        return str(
            Path.home() / ".config" / "gamdl" / "config.ini",
        )

    if os.name == "posix":
        # Although I found this from Apple: "The Application Support
        # directory ... stores any type of file that supports the app
        #  ... such as ... configuration files."
        #
        # It seems like this is the case for installed apps, not a
        # python script, thus darwin has been grouped with posix
        return str(Path.home() / ".config" / "gamdl" / "config.ini")

    try:
        return str(Path.home() / ".gamdl" / "config.ini")
    except RuntimeError:
        # Note: Path.home() failing is only checked here because gamdl
        # failing would be the least of their problems in the above
        # cases. It is checked here because gamdl may be running in an
        # environment that doesn't support Path.home(). In that case,
        # default to <cwd>/.gamdl/config.ini
        return str(Path(".gamdl") / "config.ini")
