from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from loguru import logger

_STARTUP_INTENT_VERSION = 1
_STARTUP_INTENT_FILENAME = "startup_intent.json"


@dataclass(frozen=True)
class StartupIntent:
    reason: Literal["bootstrap"]
    provider: str
    model: str


def _intent_path(db_path: str | Path) -> Path:
    return Path(db_path) / _STARTUP_INTENT_FILENAME


def write_bootstrap_startup_intent(
    db_path: str | Path,
    *,
    provider: str,
    model: str,
) -> Path:
    """Atomically record that the next process should perform a clean bootstrap start."""

    path = _intent_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _STARTUP_INTENT_VERSION,
        "reason": "bootstrap",
        "provider": provider,
        "model": model,
    }
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)
    return path


def load_bootstrap_startup_intent(
    db_path: str | Path,
    *,
    provider: str,
    model: str,
    available_providers: set[str],
) -> StartupIntent | None:
    """Load a matching bootstrap intent, clearing malformed or stale markers."""

    path = _intent_path(db_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("startup intent must be a JSON object")
        if payload.get("version") != _STARTUP_INTENT_VERSION:
            raise ValueError("unsupported startup intent version")
        if payload.get("reason") != "bootstrap":
            raise ValueError("unsupported startup intent reason")
        intent_provider = payload.get("provider")
        intent_model = payload.get("model")
        if not isinstance(intent_provider, str) or not isinstance(intent_model, str):
            raise ValueError("startup intent provider/model must be strings")
        if (
            intent_provider != provider
            or intent_model != model
            or intent_provider not in available_providers
        ):
            logger.warning("Discarding startup intent that does not match effective model config")
            path.unlink(missing_ok=True)
            return None
        return StartupIntent(
            reason="bootstrap",
            provider=intent_provider,
            model=intent_model,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        logger.warning(f"Discarding invalid startup intent: {error}")
        path.unlink(missing_ok=True)
        return None


def clear_startup_intent(db_path: str | Path) -> None:
    _intent_path(db_path).unlink(missing_ok=True)
