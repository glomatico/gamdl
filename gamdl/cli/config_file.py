import configparser
import typing
from enum import Enum
from pathlib import Path

import click

from .constants import EXCLUDED_CONFIG_FILE_PARAMS


class ConfigFile:
    def __init__(
        self,
        config_path: str,
        section_name: str = "gamdl",
    ) -> None:
        self.config_path = config_path
        self.section_name = section_name

        self._read_config_file()

    def _read_config_file(self) -> None:
        self.config = configparser.ConfigParser(interpolation=None)

        if Path(self.config_path).exists():
            self.config.read(self.config_path, encoding="utf-8")
        else:
            Path(self.config_path).parent.mkdir(parents=True, exist_ok=True)

        if not self.config.has_section(self.section_name):
            self.config.add_section(self.section_name)

    def _write_config_file(self) -> None:
        with open(self.config_path, "w", encoding="utf-8") as config_file:
            self.config.write(config_file)

    def _serialize_param_default(self, param: click.Parameter) -> str:
        if not isinstance(param.default, (list, tuple)):
            param_default = [param.default]
        else:
            param_default = param.default

        if not param_default:
            return ""

        first = param_default[0]

        if isinstance(first, Enum):
            return ",".join(str(item.value) for item in param_default)
        if isinstance(first, bool):
            return ",".join(str(item).lower() for item in param_default)
        if first is None:
            return "null"

        return ",".join(str(item) for item in param_default)

    def _add_param_default_to_config(
        self,
        param: click.Parameter,
    ) -> bool:
        if self.config[self.section_name].get(param.name):
            return False

        value = self._serialize_param_default(param)
        self.config[self.section_name][param.name] = value

        return True

    def _parse_param_from_config(
        self,
        param: click.Parameter,
    ) -> typing.Any:
        value = self.config[self.section_name].get(param.name)

        if value == "null":
            return None

        return param.type_cast_value(None, value)

    def add_params_default_to_config(
        self,
        params: list[click.Parameter],
    ) -> None:
        has_changes = False

        for param in params:
            if param.name in EXCLUDED_CONFIG_FILE_PARAMS:
                continue

            has_changes = self._add_param_default_to_config(param) or has_changes

        if has_changes:
            self._write_config_file()

    def parse_params_from_config(
        self,
        params: list[click.Parameter],
    ) -> dict[str, typing.Any]:
        parsed_params = {}

        for param in params:
            parsed_params[param.name] = self._parse_param_from_config(param)

        return parsed_params
