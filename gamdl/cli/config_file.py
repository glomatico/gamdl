import configparser
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import get_type_hints

import click
import click.types as click_types
from dataclass_click.dataclass_click import _DelayedCall

from .cli_config import CliConfig
from .constants import EXCLUDED_CONFIG_FILE_PARAMS
from .utils import Csv


@dataclass
class ParameterInfo:
    name: str
    default: typing.Any
    type: typing.Any


class ConfigFile:
    def __init__(
        self,
        config_path: str,
        section_name: str = "gamdl",
    ) -> None:
        self.config_path = config_path
        self.section_name = section_name
        self.parameters = self._extract_parameters_from_cli_config()

        self._read_config_file()

    def _extract_parameters_from_cli_config(self) -> dict[str, ParameterInfo]:
        parameters = {}
        hints = get_type_hints(CliConfig, include_extras=True)

        for field_name, hint in hints.items():
            if hasattr(hint, "__metadata__"):
                for metadata in hint.__metadata__:
                    if isinstance(metadata, _DelayedCall):
                        param_type = metadata.kwargs.get("type")
                        if param_type is None:
                            raise ValueError(
                                f"Parameter type for field '{field_name}' "
                                "could not be determined."
                            )

                        parameters[field_name] = ParameterInfo(
                            name=field_name,
                            default=metadata.kwargs.get("default"),
                            type=param_type,
                        )
                        break

        return parameters

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

    def _serialize_param_default(self, param_info: ParameterInfo) -> str:
        if param_info.default is None:
            return "null"

        if isinstance(param_info.type, Csv):
            return ",".join(
                item.value if hasattr(item, "value") else str(item)
                for item in param_info.default
            )

        if isinstance(param_info.type, click_types.FuncParamType):
            return param_info.default.value

        if isinstance(param_info.type, click_types.BoolParamType):
            return "true" if param_info.default else "false"

        if isinstance(
            param_info.type,
            click_types.Choice
            | click_types.Path
            | click_types.StringParamType
            | click_types.IntParamType,
        ):
            return str(param_info.default)

        raise NotImplementedError(
            f"Serialization for parameter '{param_info.name}' of type "
            f"'{type(param_info.type)}' is not implemented."
        )

    def _add_param_default_to_config(
        self,
        param_info: ParameterInfo,
    ) -> bool:
        if self.config.has_option(self.section_name, param_info.name):
            return False

        value = self._serialize_param_default(param_info)
        self.config.set(self.section_name, param_info.name, value)

        return True

    def _parse_param_from_config(
        self,
        param_info: ParameterInfo,
    ) -> typing.Any:
        value = self.config[self.section_name].get(param_info.name)
        if value is None:
            return param_info.default

        if value == "null":
            return None

        if not isinstance(param_info.type, click_types.ParamType):
            raise NotImplementedError(
                f"Parsing for parameter '{param_info.name}' of type "
                f"'{type(param_info.type)}' is not implemented."
            )

        return param_info.type.convert(value, None, None)

    def add_params_default_to_config(self) -> None:
        has_changes = False

        for param_info in self.parameters.values():
            if param_info.name in EXCLUDED_CONFIG_FILE_PARAMS:
                continue

            has_changes = self._add_param_default_to_config(param_info) or has_changes

        if has_changes:
            self._write_config_file()

    def cleanup_unknown_params(self) -> None:
        param_names = {info.name for info in self.parameters.values()}
        has_changes = False

        for key in list(self.config[self.section_name].keys()):
            if key not in param_names:
                self.config.remove_option(self.section_name, key)
                has_changes = True

        if has_changes:
            self._write_config_file()

    def update_params_from_config(self, config: CliConfig) -> CliConfig:
        updates = {}
        click_context = click.get_current_context()
        for param_info in self.parameters.values():
            if (
                click_context.get_parameter_source(param_info.name)
                == click.core.ParameterSource.COMMANDLINE
            ):
                continue

            if self.config.has_option(self.section_name, param_info.name):
                updates[param_info.name] = self._parse_param_from_config(param_info)

        config_dict = config.__dict__.copy()
        config_dict.update(updates)
        return CliConfig(**config_dict)
