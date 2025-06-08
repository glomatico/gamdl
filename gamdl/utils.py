from pathlib import Path

import click
import colorama
import requests

from .constants import X_NOT_FOUND_STRING


def color_text(text: str, color) -> str:
    return color + text + colorama.Style.RESET_ALL


def raise_response_exception(response: requests.Response):
    raise Exception(
        f"Request failed with status code {response.status_code}: {response.text}"
    )


def prompt_path(is_file: bool, initial_path: Path, description: str) -> Path:
    path_validator = click.Path(
        exists=True,
        file_okay=is_file,
        dir_okay=not is_file,
        path_type=Path,
    )
    while True:
        try:
            path_obj = path_validator.convert(initial_path, None, None)
            break
        except click.BadParameter as e:
            path_str = click.prompt(
                (
                    f"{X_NOT_FOUND_STRING.format(description, initial_path.absolute())} or "
                    "the specified path is not valid. "
                    "Move it to that location, type the path or drag and drop it here. "
                    "Then, press enter to continue"
                ),
                default=str(initial_path),
                show_default=False,
            )
            path_str = path_str.strip('"')
            initial_path = Path(path_str)
    return path_obj
