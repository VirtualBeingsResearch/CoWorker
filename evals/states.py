"""Life-stage workspaces: pack a snapshot and fork it for a sample."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from evals.scenario import Scenario
    from evals.workspace import ModelTarget

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


def pack(
    workspace: Path,
    name: str,
    *,
    root: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
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
    payload = {
        "name": name,
        "packed_at": datetime.now(UTC).isoformat(),
        "source": str(workspace),
    }
    if extra:
        payload.update(extra)
    (dest / "STATE.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
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


async def live(
    scenario: Scenario,
    name: str,
    target: ModelTarget,
    locale: str,
    results_dir: Path,
    *,
    root: Path | None = None,
) -> Path:
    """Run a timeline scenario once and pack the resulting workspace."""
    from evals.runner import RunOptions, run

    run_dir = await run(
        [scenario],
        RunOptions(target=target, samples=1, locales=(locale,), results_dir=results_dir),
    )
    sample = run_dir / "samples" / scenario.id / locale / "00"
    workspace = sample / "workspace"
    result_file = sample / "result.json"
    if not result_file.is_file():
        raise RuntimeError(f"live run produced no result: {run_dir}")
    result = json.loads(result_file.read_text(encoding="utf-8"))
    if result.get("status") != "completed":
        raise RuntimeError(
            f"live run did not complete: {result.get('status')}: {result.get('status_detail')}"
        )
    extra: dict[str, Any] = {
        "lived": True,
        "scenario": scenario.id,
        "locale": locale,
        "run_dir": str(run_dir),
    }
    clock_file = workspace / ".evals" / "clock.json"
    if clock_file.is_file():
        extra["clock"] = json.loads(clock_file.read_text(encoding="utf-8"))
    return pack(workspace, name, root=root, extra=extra)
