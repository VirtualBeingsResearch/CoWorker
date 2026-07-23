"""Persistent WeCom chat_id -> chat_type ("single"/"group") mapping.

Extracted from ``WeComRunner``. The runner keeps the in-memory dict; this
module owns loading/saving and legacy numeric chat_type normalization.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger


def normalize_chat_type(chat_type: Any) -> str | None:
    if chat_type in ("single", "group"):
        return chat_type
    if chat_type == 1:
        return "single"
    if chat_type == 2:
        return "group"
    return None


class ContactsStore:
    """Load/save the chat_id -> chat_type mapping to a JSON file."""

    @staticmethod
    def load(path: Path | None) -> dict[str, str]:
        if not path or not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"WeCom contacts load failed: {e}")
            return {}
        if not isinstance(raw, dict):
            return {}
        contacts: dict[str, str] = {}
        for chat_id, chat_type in raw.items():
            normalized = normalize_chat_type(chat_type)
            if normalized is not None:
                contacts[str(chat_id)] = normalized
        return contacts

    @staticmethod
    def save(path: Path | None, contacts: dict[str, str]) -> None:
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(contacts, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"WeCom contacts save failed: {e}")
