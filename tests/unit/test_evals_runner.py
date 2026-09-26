from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest
from evals import runner
from evals.driver import SampleAborted, SampleOutcome, _Sample
from evals.runner import RunOptions
from evals.scenario import Scenario, load_scenario
from evals.trace import activity
from evals.workspace import ModelTarget

SCENARIO = """
id: unit.one
kind: care
preregistration:
  hypothesis: she replies
locales: [en]
samples: 3
script:
  - from: alice
    say: hi
checks:
  - type: replied
    participant: alice
"""


def _scenario(tmp_path: Path) -> Scenario:
    path = tmp_path / "one.yaml"
    path.write_text(SCENARIO, encoding="utf-8")
    return load_scenario(path)


def _log(workspace: Path, *kinds: str) -> None:
    logs = workspace / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "interactions.jsonl").open("a", encoding="utf-8") as out:
        for kind in kinds:
            out.write(json.dumps({"type": kind}) + "\n")


def test_activity_counts_unanswered_model_calls(tmp_path: Path) -> None:
    _log(tmp_path, "message_in", "thinking_start", "thinking_start", "message_in", "thinking_start")
    assert activity(tmp_path).failed_calls == 2

    _log(tmp_path, "llm_response", "thinking_start")
    state = activity(tmp_path)
    assert state.failed_calls == 0 and state.busy


def test_repeated_model_failures_abort_the_sample(tmp_path: Path) -> None:
    sample = _Sample(_scenario(tmp_path), "en", ModelTarget("p", "m"), tmp_path)
    sample.deadline = float("inf")
    _log(sample.workspace, "message_in", "thinking_start", "thinking_start", "thinking_start")
    (tmp_path / "coworker.log").write_text(
        "21:44:33 | WARNING | Provider p/m exhausted (3 tries): Error code: 429 - quota\n"
        "21:44:33 | ERROR | Unexpected error in cycle (1/5): Error code: 429 - quota\n"
        "Traceback (most recent call last):\n",
        encoding="utf-8",
    )
    sample._check_guards()

    _log(sample.workspace, "thinking_start")
    with pytest.raises(SampleAborted) as aborted:
        sample._check_guards()
    assert aborted.value.status == "provider_error"
    assert "Unexpected error in cycle (1/5): Error code: 429 - quota" in aborted.value.detail


def test_run_halts_after_the_model_becomes_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[Path] = []

    async def fake_run_sample(
        scenario: Scenario, locale: str, target: ModelTarget, sample_dir: Path
    ) -> SampleOutcome:
        started.append(sample_dir)
        return SampleOutcome(
            status="provider_error",
            detail="quota exhausted",
            started_at=datetime.now().astimezone(),
            real_seconds=1.0,
            workspace=sample_dir / "workspace",
        )

    monkeypatch.setattr(runner, "run_sample", fake_run_sample)
    options = RunOptions(target=ModelTarget("p", "m"), results_dir=tmp_path / "results")

    run_dir = asyncio.run(runner.run([_scenario(tmp_path)], options))

    assert len(started) == 1
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["halted"] == {"status": "provider_error", "detail": "quota exhausted", "skipped": 2}
    result = json.loads((started[0] / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "provider_error" and not result["passed"]
