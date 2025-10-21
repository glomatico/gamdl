import logging

import click


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
