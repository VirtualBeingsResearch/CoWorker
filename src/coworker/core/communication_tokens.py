"""Stable names for extra communication tokens on the OpenAI channel."""

from __future__ import annotations

import re

from coworker.i18n import tr

PRIMARY_TOKEN_NAME = "api"
CONTROL_TOKEN_NAME = "control"
OPENAI_PREFIX = "openai:"
RESERVED_TOKEN_NAMES = frozenset({PRIMARY_TOKEN_NAME, CONTROL_TOKEN_NAME})
TOKEN_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,31}$")
CONTROL_PARTICIPANT_ID = f"{OPENAI_PREFIX}{CONTROL_TOKEN_NAME}"
PRIMARY_PARTICIPANT_ID = f"{OPENAI_PREFIX}{PRIMARY_TOKEN_NAME}"


def participant_id_for_token_name(name: str) -> str:
    return f"{OPENAI_PREFIX}{name}"


def token_name_from_participant(participant_id: str) -> str | None:
    if not participant_id.startswith(OPENAI_PREFIX):
        return None
    name = participant_id[len(OPENAI_PREFIX) :]
    return name or None


def validate_token_name(name: str) -> str:
    cleaned = name.strip()
    if not TOKEN_NAME_PATTERN.fullmatch(cleaned):
        raise ValueError(tr("config.api.token_name_invalid", name=name))
    if cleaned in RESERVED_TOKEN_NAMES:
        raise ValueError(tr("config.api.token_name_reserved", name=cleaned))
    return cleaned
