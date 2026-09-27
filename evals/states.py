"""Life-stage workspaces: pack a snapshot and fork it for a sample."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATES = REPO_ROOT / "evals" / "states"
_SKIP_DIRS = {"logs", "inbox", "outbox", "tmp", "__pycache__"}


def resolve(name: str) -> Path | None:
    if not name or name == "newborn":
        return None
    path = STATES / name
    if not path.is_dir():
        raise FileNotFoundError(f"life-stage state not found: {path}")
    return path


def apply(workspace: Path, state: Path) -> None:
    """Copy a packed state over the sample workspace after the newborn fixture."""
    data = state / "data"
    if data.is_dir():
        _merge(data, workspace / "data")
    coworker = state / ".coworker"
    if coworker.is_dir():
        _merge(coworker, workspace / ".coworker")


def pack(workspace: Path, name: str, *, root: Path | None = None) -> Path:
    dest = (root or STATES) / name
    if dest.exists():
        raise FileExistsError(f"state already exists: {dest}")
    dest.mkdir(parents=True)
    src_data = workspace / "data"
    if src_data.is_dir():
        shutil.copytree(src_data, dest / "data", ignore=_ignore)
    src_cw = workspace / ".coworker"
    if src_cw.is_dir():
        shutil.copytree(src_cw, dest / ".coworker", ignore=_ignore)
    (dest / "STATE.json").write_text(
        json.dumps(
            {
                "name": name,
                "packed_at": datetime.now(UTC).isoformat(),
                "source": str(workspace),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return dest


def _ignore(directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in _SKIP_DIRS or name.endswith(".log")}


def _merge(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dest / item.name
        if item.is_dir():
            if item.name in _SKIP_DIRS:
                continue
            shutil.copytree(item, target, dirs_exist_ok=True, ignore=_ignore)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
