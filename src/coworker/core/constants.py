from typing import Literal


TICK_TAG = "heartbeat"
DEFAULT_LLM_MAX_TOKENS = 8_192
DEFAULT_BUBBLE_HANDOFF_TRANSPARENCY_PARTICIPANT_MATCHES = (
    "wecom:*",
    "coworker-desktop:*:local:*",
)
DEFAULT_BUBBLE_HANDOFF_TRANSPARENCY_STREAM_TRANSPORTS: tuple[
    Literal["websocket", "sse"], ...
] = (
    "websocket",
    "sse",
)
