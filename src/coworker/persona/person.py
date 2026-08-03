"""Person, PersonAlias, PersonStore and PersonaCard.

Person is the stable anchor of a relationship: multiple channel-scoped
``participant_id`` addresses (optionally narrowed by ``conversation_id``) can be
bound to one person. The persona card is a model-maintained markdown file
capturing the agent's current understanding of that person — rewritten wholesale
so outdated knowledge can be corrected or forgotten, not just appended.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from loguru import logger

from coworker.core.ids import new_compact_id


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace ``path`` with ``content`` (tmp file + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@dataclass(frozen=True, slots=True)
class PersonAlias:
    """One reachable address of a person on one channel.

    ``conversation_id`` is only set when the channel needs it to locate the
    specific conversation or human (e.g. a WeChat session); channels that are
    uniquely routable by ``participant_id`` alone leave it ``None``.
    """

    participant_id: str
    conversation_id: str | None = None
    channel: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, str | None]:
        return {
            "participant_id": self.participant_id,
            "conversation_id": self.conversation_id,
            "channel": self.channel,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PersonAlias:
        return cls(
            participant_id=str(data.get("participant_id") or ""),
            conversation_id=data.get("conversation_id") or None,
            channel=str(data.get("channel") or ""),
            note=str(data.get("note") or ""),
        )


@dataclass
class Person:
    """A relationship the agent maintains across channels.

    ``person_id`` is opaque and stable — the future seam for an account system.
    The persona card file holds the model-maintained understanding of this
    person; this object only carries identity and addresses.
    """

    person_id: str
    display_name: str = ""
    aliases: list[PersonAlias] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "person_id": self.person_id,
            "display_name": self.display_name,
            "aliases": [alias.to_dict() for alias in self.aliases],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Person:
        return cls(
            person_id=str(data.get("person_id") or ""),
            display_name=str(data.get("display_name") or ""),
            aliases=[PersonAlias.from_dict(a) for a in data.get("aliases") or []],
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )


class PersonStore:
    """Single-file JSON store of persons, following the ``TaskStore`` pattern.

    Mutations save the whole file atomically. Read paths never touch the disk
    after load, so inbound-time lookups are cheap.
    """

    def __init__(self, store_path: str | Path = "data/persons.json") -> None:
        self._path = Path(store_path)
        self._persons: dict[str, Person] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for item in data.get("persons", []):
                person = Person.from_dict(item)
                if person.person_id:
                    self._persons[person.person_id] = person
        except Exception as e:
            logger.warning(f"Failed to load persons from {self._path}: {e}")

    def _save(self) -> None:
        payload = {"persons": [p.to_dict() for p in self._persons.values()]}
        _atomic_write_text(
            self._path,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    def get(self, person_id: str) -> Person | None:
        return self._persons.get(person_id)

    def all_persons(self) -> list[Person]:
        return list(self._persons.values())

    def find_by_participant(
        self,
        participant_id: str,
        conversation_id: str | None = None,
    ) -> Person | None:
        """Return the person bound to an address, or ``None`` if unbound.

        A generic alias (no ``conversation_id``) matches any conversation; a
        conversation-specific alias only matches that conversation.
        """
        if not participant_id:
            return None
        for person in self._persons.values():
            for alias in person.aliases:
                if alias.participant_id != participant_id:
                    continue
                if conversation_id:
                    if alias.conversation_id and alias.conversation_id != conversation_id:
                        continue
                elif alias.conversation_id:
                    continue
                return person
        return None

    def find_by_name(self, name: str) -> Person | None:
        """Return the person whose display name matches (case-insensitive)."""
        target = name.strip().lower()
        if not target:
            return None
        for person in self._persons.values():
            if person.display_name.strip().lower() == target:
                return person
        return None

    def create(
        self,
        *,
        display_name: str = "",
        aliases: list[PersonAlias] | None = None,
    ) -> Person:
        now = _now_iso()
        person = Person(
            person_id=new_compact_id(prefix="p"),
            display_name=display_name,
            aliases=list(aliases or []),
            created_at=now,
            updated_at=now,
        )
        self._persons[person.person_id] = person
        self._save()
        return person

    def update(
        self,
        person_id: str,
        *,
        display_name: str | None = None,
        aliases: list[PersonAlias] | None = None,
    ) -> Person | None:
        person = self._persons.get(person_id)
        if person is None:
            return None
        if display_name is not None:
            person.display_name = display_name
        if aliases is not None:
            person.aliases = list(aliases)
        person.updated_at = _now_iso()
        self._save()
        return person

    def delete(self, person_id: str) -> bool:
        if person_id not in self._persons:
            return False
        del self._persons[person_id]
        self._save()
        return True

    def bind_alias(self, person_id: str, alias: PersonAlias) -> Person | None:
        """Idempotently attach an address to a person."""
        person = self._persons.get(person_id)
        if person is None:
            return None
        key = (alias.participant_id, alias.conversation_id)
        for existing in person.aliases:
            if (existing.participant_id, existing.conversation_id) == key:
                return person
        person.aliases.append(alias)
        person.updated_at = _now_iso()
        self._save()
        return person

    def merge(self, keep_id: str, drop_id: str) -> Person | None:
        """Merge ``drop_id`` into ``keep_id`` (aliases union); remove ``drop_id``.

        Memory under the dropped person's scope is not moved — the agent
        re-curates it via its memory tools. Returns ``None`` on invalid input.
        """
        keep = self._persons.get(keep_id)
        drop = self._persons.get(drop_id)
        if keep is None or drop is None or keep_id == drop_id:
            return None
        existing_keys = {
            (a.participant_id, a.conversation_id) for a in keep.aliases
        }
        for alias in drop.aliases:
            key = (alias.participant_id, alias.conversation_id)
            if key not in existing_keys:
                keep.aliases.append(alias)
        if not keep.display_name and drop.display_name:
            keep.display_name = drop.display_name
        keep.updated_at = _now_iso()
        del self._persons[drop_id]
        self._save()
        return keep


class PersonaCard:
    """Per-person markdown card files, maintained by the agent (like thinking.md)."""

    def __init__(self, cards_dir: str | Path = "data/persona/cards") -> None:
        self._dir = Path(cards_dir)

    def path_for(self, person_id: str) -> Path:
        return self._dir / f"{person_id}.md"

    def load(self, person_id: str) -> str:
        path = self.path_for(person_id)
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def save(self, person_id: str, content: str) -> None:
        _atomic_write_text(self.path_for(person_id), content)

    def delete(self, person_id: str) -> None:
        self.path_for(person_id).unlink(missing_ok=True)
