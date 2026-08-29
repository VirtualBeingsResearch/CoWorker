"""Token identity and conversation stitching for the model API channel.

OpenAI-compatible clients resend the full message history on every request,
so the per-message fingerprint list of that history acts as a stable identity:
a request whose fingerprints extend (or equal) a known conversation continues
that conversation instead of starting a new one.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coworker.core.config import ModelApiTokenConfig
from coworker.core.ids import new_compact_id

_SEPARATOR = "\x1f"
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SLUG_MAX = 24
_PARTICIPANT_PREFIX = "api:"


def content_text(content: str | list[Any]) -> str:
    """Flatten an OpenAI message content into plain text."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(part for part in parts if part)


def message_fingerprint(role: str, content: str) -> str:
    """Return the stable per-message fingerprint used for history matching."""
    return hashlib.sha256(
        f"{role}{_SEPARATOR}{content.strip()}".encode()
    ).hexdigest()


@dataclass(frozen=True)
class ModelApiIdentity:
    """The participant bound to one configured model API token."""

    participant_id: str
    display_name: str
    token: str = field(repr=False)


class ModelApiTokenDirectory:
    """Static token → participant mapping built from configuration."""

    def __init__(self, tokens: list[ModelApiTokenConfig] | None = None) -> None:
        self._identities: dict[str, ModelApiIdentity] = {}
        seen: set[str] = set()
        for entry in tokens or []:
            token = entry.token.strip()
            if not token:
                continue
            participant_id = _participant_id_for(token, entry.display_name)
            if participant_id in seen:
                raise ValueError(
                    f"duplicate model API participant id: {participant_id}"
                )
            seen.add(participant_id)
            self._identities[token] = ModelApiIdentity(
                participant_id=participant_id,
                display_name=entry.display_name.strip(),
                token=token,
            )

    def resolve_authorization(self, authorization: str | None) -> ModelApiIdentity | None:
        if not authorization:
            return None
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer":
            return None
        return self._identities.get(value.strip())

    def __len__(self) -> int:
        return len(self._identities)


def _participant_id_for(token: str, display_name: str) -> str:
    slug = _SLUG_RE.sub("-", display_name.strip().lower()).strip("-")[:_SLUG_MAX]
    if slug:
        return f"{_PARTICIPANT_PREFIX}{slug}"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
    return f"{_PARTICIPANT_PREFIX}{digest}"


@dataclass
class ConversationRecord:
    """One stitched conversation: its id and the client history fingerprints."""

    conversation_id: str
    fingerprints: list[str] = field(default_factory=list)
    scenario_hash: str = ""
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "fingerprints": list(self.fingerprints),
            "scenario_hash": self.scenario_hash,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationRecord:
        return cls(
            conversation_id=str(data.get("conversation_id") or ""),
            fingerprints=[str(item) for item in data.get("fingerprints", [])],
            scenario_hash=str(data.get("scenario_hash") or ""),
            updated_at=float(data.get("updated_at") or time.time()),
        )


class ConversationRegistry:
    """Content-based conversation stitching keyed by client message history."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_conversations: int = 50,
        max_fingerprints: int = 512,
    ) -> None:
        self._path = Path(path) if path else None
        self._records: dict[str, list[ConversationRecord]] = {}
        self._loaded = False
        self._max_conversations = max_conversations
        self._max_fingerprints = max_fingerprints

    def match(
        self, participant_id: str, fingerprints: list[str]
    ) -> tuple[str, int]:
        """Return ``(conversation_id, matched_message_count)`` for a request history.

        Clients resend their full visible history, so a request continues the
        conversation whose known history shares the largest overlap with the
        request's head. ``matched_message_count`` is that overlap: the request
        extends a conversation when the head equals the whole known history
        (the common case), and trimming-aware when the head equals only the
        tail of it (a client that dropped old messages). Requests that overlap
        nothing start a new conversation.
        """
        self._ensure_loaded()
        records = self._records.setdefault(participant_id, [])
        best: ConversationRecord | None = None
        best_overlap = 0
        for record in records:
            # Cheap prefilter: the request head must exist in the known list.
            if not fingerprints or fingerprints[0] not in set(record.fingerprints):
                continue
            overlap = self._head_tail_overlap(record.fingerprints, fingerprints)
            if overlap == 0:
                continue
            if (
                best is None
                or overlap > best_overlap
                or (overlap == best_overlap and record.updated_at > best.updated_at)
            ):
                best = record
                best_overlap = overlap
        if best is None:
            record = ConversationRecord(
                conversation_id=new_compact_id(prefix="conv_"),
                fingerprints=list(fingerprints)[: self._max_fingerprints],
            )
            records.insert(0, record)
            del records[self._max_conversations :]
        else:
            record = best
            # The client's visible history becomes the canonical fingerprint
            # list going forward, so follow-up requests keep matching even
            # after the client trimmed old messages.
            record.fingerprints = list(fingerprints)[: self._max_fingerprints]
            record.updated_at = time.time()
        self._save()
        return record.conversation_id, best_overlap

    @staticmethod
    def _head_tail_overlap(known: list[str], incoming: list[str]) -> int:
        """Largest ``k`` with ``incoming[:k] == known[-k:]``."""
        max_k = min(len(known), len(incoming))
        for k in range(max_k, 0, -1):
            if known[-k:] == incoming[:k]:
                return k
        return 0

    def scenario_changed(
        self, participant_id: str, conversation_id: str, scenario_hash: str
    ) -> bool:
        """Record ``scenario_hash`` for the conversation; return True when new or changed."""
        self._ensure_loaded()
        for record in self._records.get(participant_id, []):
            if record.conversation_id == conversation_id:
                if record.scenario_hash == scenario_hash:
                    return False
                record.scenario_hash = scenario_hash
                self._save()
                return True
        return bool(scenario_hash)

    def _ensure_loaded(self) -> None:
        if self._loaded or self._path is None:
            self._loaded = True
            return
        self._loaded = True
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        participants = data.get("participants")
        if not isinstance(participants, dict):
            return
        for participant_id, entries in participants.items():
            if not isinstance(entries, list):
                continue
            self._records[participant_id] = [
                record
                for record in (
                    ConversationRecord.from_dict(entry)
                    for entry in entries
                    if isinstance(entry, dict)
                )
                if record.conversation_id
            ]

    def _save(self) -> None:
        if self._path is None:
            return
        payload = {
            "participants": {
                participant_id: [record.to_dict() for record in records]
                for participant_id, records in self._records.items()
            }
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f"{self._path.name}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, indent=2))
                stream.flush()
            temporary.replace(self._path)
        except OSError:
            temporary.unlink(missing_ok=True)
