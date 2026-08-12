from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from coworker.i18n import tr

_SCHEMA_VERSION = 1
_CHAT_KINDS = {"private", "group", "channel"}


@dataclass(frozen=True)
class TelegramContact:
    chat_id: int
    kind: str
    display_name: str = ""
    username: str = ""

    def participant_id(self, instance_id: str) -> str:
        return f"tg:{instance_id}:{self.chat_id}"


@dataclass
class TelegramState:
    bot_user_id: int | None = None
    offset: int = 0
    contacts: dict[int, TelegramContact] | None = None

    def __post_init__(self) -> None:
        if self.contacts is None:
            self.contacts = {}

    def reset_for_bot(self, bot_user_id: int) -> bool:
        if self.bot_user_id in (None, bot_user_id):
            changed = self.bot_user_id is None
            self.bot_user_id = bot_user_id
            return changed
        self.bot_user_id = bot_user_id
        self.offset = 0
        self.contacts = {}
        return True


class TelegramStateStore:
    """Persist one Bot instance's update offset and known chats without its token."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> TelegramState:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return TelegramState()
        except Exception as error:
            logger.warning(tr("channel.telegram.state_load_failed", error=error))
            return TelegramState()
        if not isinstance(raw, dict) or raw.get("version") != _SCHEMA_VERSION:
            return TelegramState()
        contacts: dict[int, TelegramContact] = {}
        for item in raw.get("contacts") or []:
            contact = _contact_from_dict(item)
            if contact is not None:
                contacts[contact.chat_id] = contact
        bot_user_id = raw.get("bot_user_id")
        offset = raw.get("offset")
        return TelegramState(
            bot_user_id=bot_user_id if isinstance(bot_user_id, int) else None,
            offset=max(0, offset) if isinstance(offset, int) else 0,
            contacts=contacts,
        )

    def save(self, state: TelegramState) -> None:
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(
                    {
                        "version": _SCHEMA_VERSION,
                        "bot_user_id": state.bot_user_id,
                        "offset": state.offset,
                        "contacts": [
                            asdict(contact)
                            for contact in sorted(
                                (state.contacts or {}).values(),
                                key=lambda item: item.chat_id,
                            )
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(self._path)
        except Exception as error:
            logger.warning(tr("channel.telegram.state_save_failed", error=error))
            temporary.unlink(missing_ok=True)


def _contact_from_dict(value: Any) -> TelegramContact | None:
    if not isinstance(value, dict):
        return None
    chat_id = value.get("chat_id")
    kind = value.get("kind")
    if not isinstance(chat_id, int) or kind not in _CHAT_KINDS:
        return None
    return TelegramContact(
        chat_id=chat_id,
        kind=kind,
        display_name=str(value.get("display_name") or ""),
        username=str(value.get("username") or ""),
    )
