import logging

import colorama

from .utils import color_text


class CustomFormatter(logging.Formatter):
    basic_format = "[%(levelname)-8s %(asctime)s]"
    formats = {
        logging.DEBUG: color_text(basic_format, colorama.Style.DIM),
        logging.INFO: color_text(basic_format, colorama.Fore.GREEN),
        logging.WARNING: color_text(basic_format, colorama.Fore.YELLOW),
        logging.ERROR: color_text(basic_format, colorama.Fore.RED),
        logging.CRITICAL: color_text(basic_format, colorama.Fore.RED),
    }
    date_format = "%H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:
        return logging.Formatter(
            self.formats.get(record.levelno) + " %(message)s",
            datefmt=self.date_format,
        ).format(record)
