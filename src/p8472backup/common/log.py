"""Write project logs for local development and deployed services."""

import logging
import os
import sys


class Log:
    """Provide the shared logging entry point for the project."""

    _logger = logging.getLogger("p8472backup")

    @classmethod
    def configure(cls) -> None:
        """Configure the process log destination from P8472BACKUP_ENV."""
        if cls._logger.handlers:
            return
        mode = os.getenv("P8472BACKUP_ENV", "prod")
        if mode not in ("dev", "intg", "prod"):
            raise ValueError("P8472BACKUP_ENV must be dev, intg, or prod")

        stderr = logging.StreamHandler()
        if mode == "dev":
            console_format = logging.Formatter("%(levelname)s %(message)s")
            stdout = logging.StreamHandler(sys.stdout)
            stdout.addFilter(lambda record: record.levelno == logging.INFO)
            stdout.setFormatter(console_format)
            stderr.setLevel(logging.WARNING)
            stderr.setFormatter(console_format)
            cls._logger.addHandler(stdout)
        else:
            stderr.setFormatter(logging.Formatter("<%(syslog_priority)d>%(message)s"))
        cls._logger.addHandler(stderr)
        cls._logger.setLevel(logging.INFO)
        cls._logger.propagate = False

    @classmethod
    def info(cls, message: str, *args: object) -> None:
        """Record an operational event."""
        cls._logger.info(message, *args, extra={"syslog_priority": 6}, stacklevel=2)

    @classmethod
    def warning(cls, message: str, *args: object) -> None:
        """Record a problem that allows the service to continue."""
        cls._logger.warning(message, *args, extra={"syslog_priority": 4}, stacklevel=2)

    @classmethod
    def error(cls, message: str, *args: object) -> None:
        """Record a fatal service error."""
        cls._logger.error(message, *args, extra={"syslog_priority": 3}, stacklevel=2)
