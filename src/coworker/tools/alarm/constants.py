from __future__ import annotations

MAX_CHECKS_PER_HOUR = 60
MIN_CONDITION_INTERVAL_SECONDS = 60
DEFAULT_INTERVAL_SECONDS = 300
MAX_MESSAGE_CHARS = 2000
CONDITION_KINDS: frozenset[str] = frozenset(
    {"file_changed", "http_changed", "task_state"}
)
