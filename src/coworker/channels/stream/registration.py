"""Persistent participant registration store for the stream channel.

Extracted from ``CommunicateTool``. Persists ``communicate_registrations.json``
and assigns server-side participant_ids (the desktop ``:d:{actor}:`` special
case is preserved -- it is a wire contract the Rust bridge relies on).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from loguru import logger

from coworker.core.ids import new_compact_id
from coworker.core.types import CommunicateRegistration

_SAFE_PARTICIPANT_CHARS_RE = re.compile(r"[^a-zA-Z0-9_.:-]+")


class RegistrationStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> list[CommunicateRegistration]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception as error:
            logger.warning(f"Failed to read communicate registrations: {error}")
            return []
        if not isinstance(data, list):
            return []
        return [CommunicateRegistration.from_dict(item) for item in data if isinstance(item, dict)]

    def save(self, registrations: list[CommunicateRegistration]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f".{self._path.name}.tmp")
        tmp.write_text(
            json.dumps(
                [item.to_dict(active=False) for item in registrations if item.participant_id],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(self._path)


def next_participant_id(
    kind: str,
    client_id: str,
    registrations: list[CommunicateRegistration],
    live_ids: set[str],
) -> str:
    """Assign a fresh, non-colliding participant_id for a new registration.

    ``live_ids`` is the set of currently-connected stream participant_ids so a
    new id never collides with a live connection.
    """
    used = {item.participant_id for item in registrations}
    prefix = _SAFE_PARTICIPANT_CHARS_RE.sub("-", kind).strip(".:-") or "unknown"
    client_segment = _SAFE_PARTICIPANT_CHARS_RE.sub("-", client_id).strip(".:-") or "unknown"
    if prefix == "coworker-desktop":
        # Desktop metadata already stores desktop_id/coworker_id.  Keep only
        # the actor segment required by handoff matching instead of copying
        # the full client_id into every model-visible participant ID.
        client_parts = client_segment.split(":")
        actor_segment = client_parts[1] if len(client_parts) > 1 else "unknown"
        base = f"{prefix}:d:{actor_segment}"
    else:
        base = prefix
    while True:
        candidate = f"{base}:{new_compact_id(entropy_bytes=6)}"
        if candidate not in used and candidate not in live_ids:
            return candidate


def build_registration(
    *,
    kind: str,
    client_id: str,
    display_name: str,
    metadata: dict,
    participant_id: str,
) -> CommunicateRegistration:
    now = datetime.now().isoformat()
    return CommunicateRegistration(
        registration_id=uuid.uuid4().hex,
        participant_id=participant_id,
        kind=kind,
        client_id=client_id,
        display_name=display_name or client_id,
        created_at=now,
        last_registered_at=now,
        metadata=metadata or {},
    )
