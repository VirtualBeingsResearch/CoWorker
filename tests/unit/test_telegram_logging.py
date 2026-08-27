from __future__ import annotations

import logging

from coworker.channels.telegram.logging import configure_telegram_polling_logs


def test_telegram_polling_http_logs_only_downgrade_poll_requests() -> None:
    configure_telegram_polling_logs()
    updates = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        "HTTP Request: POST https://api.telegram.org/bot123:abc/getUpdates \"HTTP/1.1 200 OK\"",
        (),
        None,
    )
    send_message = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        "HTTP Request: POST https://api.telegram.org/bot123:abc/sendMessage \"HTTP/1.1 200 OK\"",
        (),
        None,
    )

    for logger_name, record in (
        ("httpx", updates),
        ("httpx", send_message),
    ):
        for log_filter in logging.getLogger(logger_name).filters:
            log_filter.filter(record)

    assert updates.levelno == logging.DEBUG
    assert send_message.levelno == logging.INFO
