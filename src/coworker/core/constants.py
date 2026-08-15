from typing import Literal

TICK_TAG = "heartbeat"
DEFAULT_LLM_MAX_TOKENS = 8_192

# 跨 provider 统一的思考强度档位。不同 provider 可支持的子集会由各自的
# thinking 映射裁剪/映射；空字符串表示使用 provider 默认强度。
ThinkingEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
THINKING_EFFORT_LEVELS: tuple[ThinkingEffort, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)
DEFAULT_BUBBLE_HANDOFF_TRANSPARENCY_PARTICIPANT_MATCHES = (
    "wecom:*",
    "weixin:*",
    "tg:*",
    "coworker-desktop:*:local:*",
)
DEFAULT_BUBBLE_HANDOFF_TRANSPARENCY_STREAM_TRANSPORTS: tuple[
    Literal["websocket", "sse"], ...
] = (
    "websocket",
    "sse",
)
