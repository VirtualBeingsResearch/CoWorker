from __future__ import annotations

import json
from pathlib import Path

import pytest
from evals.report import compare, summarize, write_report
from evals.stats import intervals_disjoint, wilson_interval


def test_wilson_interval_bounds() -> None:
    assert wilson_interval(0, 0) == (0.0, 1.0)
    low, high = wilson_interval(5, 5)
    assert high == pytest.approx(1.0) and 0.5 < low < 0.6
    low, high = wilson_interval(0, 5)
    assert low == pytest.approx(0.0) and 0.4 < high < 0.5
    low, high = wilson_interval(50, 100)
    assert low < 0.5 < high


def test_intervals_disjoint() -> None:
    assert intervals_disjoint((0.0, 0.3), (0.5, 1.0))
    assert not intervals_disjoint((0.0, 0.6), (0.5, 1.0))


def _sample(scenario: str, passed: bool, status: str = "completed") -> dict:
    return {
        "scenario": scenario,
        "locale": "en",
        "status": status,
        "passed": passed,
        "checks": [{"name": "replied", "passed": passed, "detail": ""}],
        "llm_calls": 4,
        "real_seconds": 10.0,
    }


def test_summarize_counts_failures_and_statuses() -> None:
    rows = summarize([_sample("a", True), _sample("a", False), _sample("a", False, "timeout")])
    (row,) = rows
    assert (row["passes"], row["samples"], row["pass_all"]) == (1, 3, False)
    assert row["failures"] == {"replied": 2, "status:timeout": 1}


def test_unreachable_model_samples_are_not_counted(tmp_path: Path) -> None:
    rows = summarize([_sample("a", True), _sample("a", False, "provider_error")])
    (row,) = rows
    assert (row["passes"], row["samples"], row["pass_all"]) == (1, 1, True)
    assert row["unobserved"] == {"provider_error": 1} and row["failures"] == {}

    (empty,) = summarize([_sample("b", False, "provider_error")])
    assert (empty["samples"], empty["pass_all"], empty["mean_llm_calls"]) == (0, False, 0.0)
    assert compare([empty], summarize([_sample("b", True)])) == []

    meta = {"run_id": "r1", "halted": {"status": "provider_error", "detail": "quota", "skipped": 4}}
    (tmp_path / "run.json").write_text(json.dumps(meta), "utf-8")
    sample_dir = tmp_path / "samples" / "b" / "en" / "00"
    sample_dir.mkdir(parents=True)
    (sample_dir / "result.json").write_text(
        json.dumps(_sample("b", False, "provider_error")), "utf-8"
    )
    markdown = write_report(tmp_path)
    assert "**halted** (`provider_error`), 4 samples not started: quota" in markdown
    assert "| b | en | 0/0 |" in markdown and "provider_error×1 |" in markdown


def test_compare_flags_only_disjoint_changes() -> None:
    good = summarize([_sample("a", True)] * 20)
    bad = summarize([_sample("a", False)] * 20)
    slightly = summarize([_sample("a", True)] * 18 + [_sample("a", False)] * 2)
    (regression,) = compare(bad, good)
    assert regression["regression"] and not regression["improvement"]
    (noise,) = compare(slightly, good)
    assert not noise["regression"] and noise["delta"] < 0


def test_write_report(tmp_path: Path) -> None:
    (tmp_path / "run.json").write_text(json.dumps({"run_id": "r1", "provider": "p"}), "utf-8")
    sample_dir = tmp_path / "samples" / "a" / "en" / "00"
    sample_dir.mkdir(parents=True)
    (sample_dir / "result.json").write_text(json.dumps(_sample("a", True)), "utf-8")
    markdown = write_report(tmp_path)
    assert "| a | en | 1/1 |" in markdown
    assert json.loads((tmp_path / "summary.json").read_text("utf-8"))["rows"][0]["passes"] == 1
