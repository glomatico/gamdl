import configparser
import typing
from pathlib import Path

import click
from click.types import BoolParamType, FuncParamType

from .constants import EXCLUDED_CONFIG_FILE_PARAMS
from .utils import Csv


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
        if param.default is None:
            return "null"

        if isinstance(param.type, Csv):
            return ",".join(item.value for item in param.default)

        if isinstance(param.type, BoolParamType):
            return str(param.default).lower()

        if isinstance(param.type, FuncParamType):
            return param.default.value

        return str(param.default)

    def _add_param_default_to_config(
        self,
        param: click.Parameter,
    ) -> bool:
        if self.config.has_option(self.section_name, param.name):
            return False

        value = self._serialize_param_default(param)
        self.config.set(self.section_name, param.name, value)

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

    def cleanup_unknown_params(
        self,
        params: list[click.Parameter],
    ) -> None:
        param_names = {param.name for param in params}
        has_changes = False

        for key in list(self.config[self.section_name].keys()):
            if key not in param_names:
                self.config.remove_option(self.section_name, key)
                has_changes = True

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
