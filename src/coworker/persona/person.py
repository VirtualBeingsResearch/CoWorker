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
from coworker.i18n import tr


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
    ``notes`` accumulate per address — the same channel may carry several
    notes (added over time by the agent when binding).
    """

    participant_id: str
    conversation_id: str | None = None
    channel: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "participant_id": self.participant_id,
            "conversation_id": self.conversation_id,
            "channel": self.channel,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict) -> PersonAlias:
        notes = data.get("notes")
        if not isinstance(notes, list):
            # 兼容旧格式：单条字符串 note。
            legacy = data.get("note")
            notes = [str(legacy)] if legacy else []
        return cls(
            participant_id=str(data.get("participant_id") or ""),
            conversation_id=data.get("conversation_id") or None,
            channel=str(data.get("channel") or ""),
            notes=[str(n) for n in notes if str(n).strip()],
        )


@dataclass
class Person:
    """A relationship the agent maintains across channels.

    ``person_id`` is opaque and stable — the future seam for an account system.
    Personalized information lives in ``notes``: person-level notes recorded by
    the agent through the persona tool, plus per-address notes on each alias.
    The persona card is a framework rendered from this structured data.
    """

    person_id: str
    display_name: str = ""
    notes: list[str] = field(default_factory=list)
    aliases: list[PersonAlias] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "person_id": self.person_id,
            "display_name": self.display_name,
            "notes": list(self.notes),
            "aliases": [alias.to_dict() for alias in self.aliases],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Person:
        notes = data.get("notes")
        if not isinstance(notes, list):
            notes = []
        return cls(
            person_id=str(data.get("person_id") or ""),
            display_name=str(data.get("display_name") or ""),
            notes=[str(n) for n in notes if str(n).strip()],
            aliases=[PersonAlias.from_dict(a) for a in data.get("aliases") or []],
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )


class PersonStore:
    """Single-file JSON store of persons, following the ``TaskStore`` pattern.

    Mutations save the whole file atomically. Read paths never touch the disk
    after load, so inbound-time lookups are cheap.
    """

    def __init__(self, store_path: str | Path = "data/memory/persons.json") -> None:
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
        notes: list[str] | None = None,
        aliases: list[PersonAlias] | None = None,
    ) -> Person:
        now = _now_iso()
        person = Person(
            person_id=new_compact_id(prefix="p"),
            display_name=display_name,
            notes=list(notes or []),
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
        notes: list[str] | None = None,
        aliases: list[PersonAlias] | None = None,
    ) -> Person | None:
        person = self._persons.get(person_id)
        if person is None:
            return None
        if display_name is not None:
            person.display_name = display_name
        if notes is not None:
            person.notes = list(notes)
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
        """Idempotently attach an address to a person.

        When the address already exists, the new alias's notes are merged into
        the existing notes (deduplicated) — so the same channel can accumulate
        several notes over time.
        """
        person = self._persons.get(person_id)
        if person is None:
            return None
        key = (alias.participant_id, alias.conversation_id)
        for existing in person.aliases:
            if (existing.participant_id, existing.conversation_id) == key:
                merged = False
                for note in alias.notes:
                    if note not in existing.notes:
                        existing.notes.append(note)
                        merged = True
                if merged:
                    person.updated_at = _now_iso()
                    self._save()
                return person
        person.aliases.append(alias)
        person.updated_at = _now_iso()
        self._save()
        return person

    def unbind_alias(
        self,
        person_id: str,
        participant_id: str,
        conversation_id: str | None = None,
    ) -> bool:
        """Remove an address from a person; returns whether it was removed.

        Matching follows the same rule as :meth:`find_by_participant`: a
        generic alias (no ``conversation_id``) matches any conversation; a
        conversation-specific alias only matches that conversation.
        """
        person = self._persons.get(person_id)
        if person is None:
            return False
        for index, alias in enumerate(person.aliases):
            if alias.participant_id != participant_id:
                continue
            if conversation_id:
                if alias.conversation_id and alias.conversation_id != conversation_id:
                    continue
            elif alias.conversation_id:
                continue
            del person.aliases[index]
            person.updated_at = _now_iso()
            self._save()
            return True
        return False

    def add_note(self, person_id: str, note: str) -> Person | None:
        """Append a person-level note (deduplicated)."""
        person = self._persons.get(person_id)
        if person is None:
            return None
        note = note.strip()
        if note and note not in person.notes:
            person.notes.append(note)
            person.updated_at = _now_iso()
            self._save()
        return person

    def remove_note(self, person_id: str, note: str) -> Person | None:
        """Remove a person-level note (forgetting outdated knowledge)."""
        person = self._persons.get(person_id)
        if person is None:
            return None
        if note in person.notes:
            person.notes.remove(note)
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
        for note in drop.notes:
            if note not in keep.notes:
                keep.notes.append(note)
        if not keep.display_name and drop.display_name:
            keep.display_name = drop.display_name
        keep.updated_at = _now_iso()
        del self._persons[drop_id]
        self._save()
        return keep


class PersonaCard:
    """Render the persona card framework from a Person's structured data.

    The card is a framework: the layout is provided by the system, while the
    personalized content lives in the person's ``notes`` and each address's
    notes, recorded by the agent through the persona tool. There is no
    free-form card file — the card is derived data.
    """

    def render(self, person: Person) -> str:
        """Render the framework card, or ``""`` when nothing is recorded.

        The framework layout: a header with the display name, a person-level
        notes section, and an address section listing every bound address
        (with its notes when present). Only sections with content are emitted.
        """
        has_content = bool(
            person.display_name or person.notes or person.aliases
        )
        if not has_content:
            return ""
        lines: list[str] = [
            tr(
                "persona.card_header",
                name=person.display_name or person.person_id,
                person_id=person.person_id,
            )
        ]
        if person.notes:
            lines.append(tr("persona.card_notes_title"))
            lines.extend(tr("persona.card_note_item", note=n) for n in person.notes)
        if person.aliases:
            lines.append(tr("persona.card_aliases_title"))
            for alias in person.aliases:
                conversation = (
                    tr(
                        "persona.card_alias_conversation",
                        conversation_id=alias.conversation_id,
                    )
                    if alias.conversation_id
                    else ""
                )
                lines.append(
                    tr(
                        "persona.card_alias_plain",
                        participant=alias.participant_id,
                        conversation=conversation,
                    )
                )
                # 每条备注独占一行（与人物级备注同构），多条备注清晰可辨。
                for note in alias.notes:
                    lines.append(tr("persona.card_alias_note_item", note=note))
        if person.updated_at:
            # 新鲜度信号：让模型知道这份认知多久没被维护，据此决定是否补充/修正。
            lines.append(tr("persona.card_updated_at", ts=person.updated_at))
        return "\n".join(lines)
