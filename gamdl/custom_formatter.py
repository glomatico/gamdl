import logging

import colorama

from .utils import color_text


class CustomFormatter(logging.Formatter):
    base_format = "[%(levelname)-8s %(asctime)s]"
    format_colors = {
        logging.DEBUG: colorama.Style.DIM,
        logging.INFO: colorama.Fore.GREEN,
        logging.WARNING: colorama.Fore.YELLOW,
        logging.ERROR: colorama.Fore.RED,
        logging.CRITICAL: colorama.Fore.RED,
    }
    date_format = "%H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:
        return logging.Formatter(
            color_text(self.base_format, self.format_colors.get(record.levelno))
            + " %(message)s",
            datefmt=self.date_format,
        ).format(record)
