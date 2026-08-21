"""Persistent WeCom chat_id -> chat_type ("single"/"group") mapping.

Extracted from ``WeComRunner``. The runner keeps the in-memory dict; this
module owns loading/saving, per-instance file naming, legacy numeric
chat_type normalization, and the one-time migration of the legacy
single-instance ``wecom_contacts.json`` into the ``default`` instance file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

DEFAULT_INSTANCE_FILE = "wecom_contacts_default.json"
LEGACY_FILE = "wecom_contacts.json"


def normalize_chat_type(chat_type: Any) -> str | None:
    if chat_type in ("single", "group"):
        return chat_type
    if chat_type == 1:
        return "single"
    if chat_type == 2:
        return "group"
    return None


def _normalized(raw: dict[Any, Any]) -> dict[str, str]:
    contacts: dict[str, str] = {}
    for chat_id, chat_type in raw.items():
        value = normalize_chat_type(chat_type)
        if value is not None:
            contacts[str(chat_id)] = value
    return contacts


class ContactsStore:
    """Load/save the chat_id -> chat_type mapping to a JSON file."""

    @staticmethod
    def load(path: Path | None) -> dict[str, str]:
        if not path:
            return {}
        if path.exists():
            return ContactsStore._read(path)
        # 一次性迁移：旧版单实例 wecom_contacts.json → default 实例文件。
        # 仅对 default 实例触发；迁移后旧文件保留作为备份，default 文件成为权威来源。
        if path.name == DEFAULT_INSTANCE_FILE:
            legacy = path.parent / LEGACY_FILE
            if legacy.exists():
                contacts = ContactsStore._read(legacy)
                if contacts:
                    ContactsStore.save(path, contacts)
                    return contacts
        return {}

    @staticmethod
    def _read(path: Path | None) -> dict[str, str]:
        if not path or not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"WeCom contacts load failed: {e}")
            return {}
        if not isinstance(raw, dict):
            return {}
        return _normalized(raw)

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
