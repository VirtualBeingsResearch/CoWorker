from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from loguru import logger

_SCHEMA_VERSION = 1


class ChannelActivityStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._participants = self._load()

    def record_sent(self, participant_id: str) -> None:
        self._record(participant_id, "last_sent_at")

    def record_received(self, participant_id: str) -> None:
        self._record(participant_id, "last_received_at")

    def activity_for(self, participant_id: str) -> tuple[str | None, str | None]:
        with self._lock:
            activity = self._participants.get(participant_id, {})
            return activity.get("last_sent_at"), activity.get("last_received_at")

    def _record(self, participant_id: str, field: str) -> None:
        participant_id = participant_id.strip()
        if not participant_id:
            return
        with self._lock:
            activity = self._participants.setdefault(participant_id, {})
            activity[field] = datetime.now().astimezone().isoformat(timespec="seconds")
            self._save()

    def _load(self) -> dict[str, dict[str, str]]:
        if self._path is None:
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as error:
            logger.warning(f"Failed to read channel activity: {error}")
            return {}
        if not isinstance(payload, dict) or payload.get("version") != _SCHEMA_VERSION:
            return {}
        participants = payload.get("participants")
        if not isinstance(participants, dict):
            return {}
        return {
            str(participant_id): {
                key: value
                for key, value in activity.items()
                if key in {"last_sent_at", "last_received_at"} and isinstance(value, str)
            }
            for participant_id, activity in participants.items()
            if isinstance(activity, dict)
        }

    def _save(self) -> None:
        if self._path is None:
            return
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(
                    {
                        "version": _SCHEMA_VERSION,
                        "participants": self._participants,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(self._path)
        except Exception as error:
            logger.warning(f"Failed to persist channel activity: {error}")
            temporary.unlink(missing_ok=True)
