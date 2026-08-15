from __future__ import annotations

from coworker.core.constants import THINKING_EFFORT_LEVELS, ThinkingEffort
from coworker.i18n import tr


def normalize_thinking_effort(value: str | None) -> ThinkingEffort | None:
    """Normalize a configured canonical effort value, keeping ``""``/None as unset."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text not in THINKING_EFFORT_LEVELS:
        raise ValueError(
            tr(
                "config.thinking.effort_invalid",
                value=str(value),
                levels=", ".join(THINKING_EFFORT_LEVELS),
            )
        )
    return text


def resolve_effort(thinking: bool, thinking_effort: str | None) -> ThinkingEffort | None:
    """Resolve the effective canonical effort for one provider call.

    ``thinking=False`` always disables thinking ("none"); otherwise the
    configured effort is normalized and returned, with ``None`` meaning
    "provider default" so existing call shapes remain unchanged.
    """
    if not thinking:
        return "none"
    return normalize_thinking_effort(thinking_effort)
