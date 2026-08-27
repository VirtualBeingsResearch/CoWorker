from __future__ import annotations

import logging

_GET_UPDATES_PATH = "/getUpdates"


def _is_polling_message(message: str) -> bool:
    return _GET_UPDATES_PATH in message


class _PollingLogLevelFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno == logging.INFO and _is_polling_message(record.getMessage()):
            record.levelno = logging.DEBUG
            record.levelname = logging.getLevelName(logging.DEBUG)
        return True


def configure_telegram_polling_logs() -> None:
    """Downgrade the long-poll ``getUpdates`` HTTP request log to ``DEBUG``.

    ``python-telegram-bot`` routes every ``getUpdates`` call through an
    ``httpx`` client, and ``httpx`` logs each request at ``INFO``. Because long
    polling runs continuously, this would flood ``INFO`` logs with one entry per
    poll; a filter attached to the ``httpx`` logger mutates matching records to
    ``DEBUG`` before they reach Loguru's sinks.
    """
    for logger_name in ("httpx",):
        dependency_logger = logging.getLogger(logger_name)
        if not any(
            isinstance(log_filter, _PollingLogLevelFilter)
            for log_filter in dependency_logger.filters
        ):
            dependency_logger.addFilter(_PollingLogLevelFilter())
