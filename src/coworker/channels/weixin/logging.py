from __future__ import annotations

import logging

_UPDATES_PATH = "/ilink/bot/getupdates"
_MANAGEMENT_SNAPSHOT_PATH = "/api/admin/channels/weixin/management"


def _is_polling_message(message: str) -> bool:
    return _UPDATES_PATH in message or (
        _MANAGEMENT_SNAPSHOT_PATH in message and "GET" in message
    )


class _PollingLogLevelFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno == logging.INFO and _is_polling_message(record.getMessage()):
            record.levelno = logging.DEBUG
            record.levelname = logging.getLevelName(logging.DEBUG)
        return True


def configure_weixin_polling_logs() -> None:
    for logger_name in ("httpx", "uvicorn.access"):
        dependency_logger = logging.getLogger(logger_name)
        if not any(
            isinstance(log_filter, _PollingLogLevelFilter)
            for log_filter in dependency_logger.filters
        ):
            dependency_logger.addFilter(_PollingLogLevelFilter())
