from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest
from evals import runner
from evals.cli import main
from evals.driver import SampleOutcome
from evals.experiment import (
    FALSIFIED,
    INCONCLUSIVE,
    SUPPORTED,
    UNOBSERVED,
    conclude_locale,
    experiment_meta,
    materialize,
)
from evals.report import write_report
from evals.runner import RunOptions
from evals.scenario import Scenario, load_scenario
from evals.workspace import REPO_ROOT, ModelTarget

from tests.unit.test_evals_scenario import EXPERIMENT, MINIMAL, _write


def _spec() -> dict:
    return {
        "contrast": {
            "check": "jasmine tea recalled",
            "present": ["seeded"],
            "absent": ["newborn"],
        },
        "arms": [{"id": "seeded", "state": "seeded"}, {"id": "newborn", "state": "newborn"}],
    }


def _sample(arm: str, contrast_passed: bool | None, status: str = "completed") -> dict:
    checks = [{"name": "replied", "passed": status == "completed", "detail": ""}]
    if contrast_passed is not None:
        checks.append(
            {"name": "jasmine tea recalled", "passed": contrast_passed, "detail": ""}
        )
    return {
        "scenario": "demo.experiment",
        "locale": "en",
        "arm": arm,
        "status": status,
        "passed": status == "completed" and contrast_passed is True,
        "checks": checks,
        "llm_calls": 2,
        "real_seconds": 8.0,
    }


def test_materialize_copies_state_onto_each_arm(tmp_path: Path) -> None:
    scenario = load_scenario(_write(tmp_path, EXPERIMENT))
    arms = materialize(scenario)
    assert [item.arm for item in arms] == ["seeded", "newborn"]
    assert [item.state for item in arms] == ["seeded", "newborn"]
    assert all(item.id == "demo.experiment" for item in arms)
    meta = experiment_meta(scenario)
    assert meta["contrast"]["check"] == "jasmine tea recalled"
    assert meta["falsified_if"]


def test_conclude_supported_falsified_unobserved_inconclusive() -> None:
    spec = _spec()
    supported = conclude_locale(
        "demo.experiment",
        "en",
        spec,
        [_sample("seeded", True), _sample("newborn", False)],
    )
    assert supported["conclusion"] == SUPPORTED
    assert supported["arms"]["seeded"]["contrast_passed"] is True
    assert supported["arms"]["newborn"]["contrast_passed"] is False

    falsified = conclude_locale(
        "demo.experiment",
        "en",
        spec,
        [_sample("seeded", False), _sample("newborn", False)],
    )
    assert falsified["conclusion"] == FALSIFIED

    leaked = conclude_locale(
        "demo.experiment",
        "en",
        spec,
        [_sample("seeded", True), _sample("newborn", True)],
    )
    assert leaked["conclusion"] == FALSIFIED

    unobserved = conclude_locale(
        "demo.experiment",
        "en",
        spec,
        [_sample("seeded", True), _sample("newborn", False, "provider_error")],
    )
    assert unobserved["conclusion"] == UNOBSERVED

    missing = conclude_locale("demo.experiment", "en", spec, [_sample("seeded", True)])
    assert missing["conclusion"] == INCONCLUSIVE
    assert "missing arm" in missing["detail"]

    mixed = conclude_locale(
        "demo.experiment",
        "en",
        spec,
        [_sample("seeded", True), _sample("seeded", False), _sample("newborn", False)],
    )
    assert mixed["conclusion"] == INCONCLUSIVE


def test_write_report_uses_conclusions_not_pass_rates(tmp_path: Path) -> None:
    scenario = load_scenario(_write(tmp_path, EXPERIMENT))
    meta = {
        "run_id": "r-exp",
        "kind": "experiment",
        "provider": "p",
        "model": "m",
        "experiments": {scenario.id: experiment_meta(scenario)},
    }
    (tmp_path / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    for arm, passed in (("seeded", True), ("newborn", False)):
        sample_dir = tmp_path / "samples" / scenario.id / arm / "en" / "00"
        sample_dir.mkdir(parents=True)
        (sample_dir / "result.json").write_text(
            json.dumps(_sample(arm, passed)), encoding="utf-8"
        )
    markdown = write_report(tmp_path)
    assert "Conclusions are control contrasts, not pass rates." in markdown
    assert "**conclusion: supported**" in markdown
    assert "| seeded | completed | yes |" in markdown
    assert "| newborn | completed | no |" in markdown
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert "rows" not in summary
    assert summary["conclusions"][0]["conclusion"] == SUPPORTED


def test_experiment_run_writes_arm_sample_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[Path] = []

    async def fake_run_sample(
        scenario: Scenario, locale: str, target: ModelTarget, sample_dir: Path
    ) -> SampleOutcome:
        started.append(sample_dir)
        return SampleOutcome(
            status="completed",
            detail="",
            started_at=datetime.now().astimezone(),
            real_seconds=1.0,
            workspace=sample_dir / "workspace",
        )

    monkeypatch.setattr(runner, "run_sample", fake_run_sample)
    scenario = load_scenario(_write(tmp_path, EXPERIMENT))
    options = RunOptions(
        target=ModelTarget("p", "m"),
        locales=("en",),
        results_dir=tmp_path / "results",
    )
    run_dir = asyncio.run(runner.run(materialize(scenario), options))
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["kind"] == "experiment"
    assert meta["experiments"][scenario.id]["contrast"]["check"] == "jasmine tea recalled"
    paths = {path.relative_to(run_dir / "samples").as_posix() for path in started}
    assert paths == {
        "demo.experiment/seeded/en/00",
        "demo.experiment/newborn/en/00",
    }
    seeded = json.loads(
        (run_dir / "samples" / "demo.experiment" / "seeded" / "en" / "00" / "result.json").read_text(
            encoding="utf-8"
        )
    )
    assert seeded["arm"] == "seeded" and seeded["kind"] == "experiment"


def test_cli_separates_care_run_from_experiments(tmp_path: Path) -> None:
    experiment = _write(tmp_path, EXPERIMENT, "experiment.yaml")
    care = _write(tmp_path, MINIMAL, "care.yaml")
    assert main(["run", str(experiment), "--provider", "p", "--model", "m"]) == 2
    assert main(["experiments", str(care), "--provider", "p", "--model", "m"]) == 2
    assert main(["check", str(REPO_ROOT / "evals" / "scenarios" / "experiments")]) == 0
