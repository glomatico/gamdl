import logging
from termcolor import colored


class CustomFormatter(logging.Formatter):
    basic_format = "[%(levelname)-8s %(asctime)s]"
    formats = {
        logging.DEBUG: colored(basic_format, "grey"),
        logging.INFO: colored(basic_format, "green"),
        logging.WARNING: colored(basic_format, "yellow"),
        logging.ERROR: colored(basic_format, "red"),
        logging.CRITICAL: colored(basic_format, "red"),
    }
    date_format = "%H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:
        return logging.Formatter(
            self.formats.get(record.levelno) + " %(message)s",
            datefmt=self.date_format,
        ).format(record)
