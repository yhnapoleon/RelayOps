"""
Centralized logging configuration for the RelayOps backend.

Provides setup_logging() to initialize the logging system and get_logger()
for consistent logger creation across modules. Uses loguru for structured,
human-readable log output.
"""

import logging
import sys
from typing import Optional

from loguru import logger

_logging_initialized = False


class _InterceptHandler(logging.Handler):
    """Route stdlib logging (uvicorn, sqlalchemy, etc.) through loguru."""

    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.bind(name=record.name).opt(depth=6, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(level: Optional[str] = None, format_str: Optional[str] = None) -> None:
    """
    Initialize the logging system with the specified configuration.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
               Defaults to config value or INFO.
        format_str: Log format string. Defaults to config value or standard format.
    """
    global _logging_initialized

    if _logging_initialized:
        return

    from core.config import get_config

    config = get_config()

    log_level = level or config.log_level
    log_format = format_str or config.log_format

    # Configure loguru.
    #
    # enqueue=True is critical on the single-process CML deployment: it hands
    # every record to a dedicated writer thread via a queue instead of writing
    # to stdout on the calling thread. Without it, if the CML log collector
    # reading our stdout stalls (pipe buffer fills), the *next* logging call on
    # any thread blocks on the stdout write — and because the monitoring tick,
    # every sync request worker, and the event loop all log, the whole process
    # wedges into a permanent "loading" state that only a restart clears.
    logger.remove()
    logger.add(
        sys.stdout,
        level=log_level.upper(),
        format=log_format,
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )

    # Intercept stdlib logging so third-party libs route through loguru
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)

    # These libraries emit one INFO line per outbound HTTP call / LDAP op. The
    # monitoring tick fans out hundreds of httpx calls per cycle, so at INFO
    # they flood stdout (the very back-pressure enqueue=True guards against —
    # but the cheapest fix is to not emit the noise at all). Pin them to
    # WARNING so only failures surface.
    for _noisy in ("httpx", "httpcore", "urllib3", "ldap3"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    _logging_initialized = True


def get_logger(name: str):
    """
    Get a logger instance with the specified name.

    Args:
        name: Logger name, typically __name__ of the calling module.

    Returns:
        A loguru logger bound with the given name.
    """
    return logger.bind(name=name)
