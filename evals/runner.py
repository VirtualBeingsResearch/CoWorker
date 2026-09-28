"""Schedule samples, grade them, and record run metadata."""

from __future__ import annotations

import asyncio
import json
import platform
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from evals.driver import SampleOutcome, run_sample
from evals.graders import CheckContext, run_check
from evals.report import UNOBSERVED_STATUSES
from evals.scenario import Scenario
from evals.trace import collect
from evals.workspace import REPO_ROOT, ModelTarget

DEFAULT_RESULTS_DIR = REPO_ROOT / "evals" / "results"


@dataclass(frozen=True)
class RunOptions:
    target: ModelTarget
    samples: int | None = None
    locales: tuple[str, ...] | None = None
    jobs: int = 1
    results_dir: Path = DEFAULT_RESULTS_DIR


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _coworker_version() -> str:
    version_file = REPO_ROOT / "VERSION"
    return version_file.read_text(encoding="utf-8").strip() if version_file.is_file() else ""


def _without_credentials(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def run_metadata(run_id: str, scenarios: list[Scenario], options: RunOptions) -> dict[str, Any]:
    kinds = {item.kind for item in scenarios}
    originals: dict[str, Scenario] = {}
    for item in scenarios:
        originals.setdefault(item.id, item)
    meta: dict[str, Any] = {
        "run_id": run_id,
        "kind": kinds.pop() if len(kinds) == 1 else "mixed",
        "started_at": datetime.now(UTC).isoformat(),
        "git_sha": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
        "coworker_version": _coworker_version(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "provider": options.target.provider,
        "model": options.target.model,
        "providers_file": bool(options.target.providers_file),
        "base_urls": {
            name: _without_credentials(url) for name, url in options.target.base_urls.items()
        },
        "scenarios": {
            item.id: {
                "version": item.version,
                "hash": item.content_hash,
                "source": str(item.source),
            }
            for item in originals.values()
        },
    }
    experiments = {
        item.id: _experiment_meta(item)
        for item in originals.values()
        if item.kind == "experiment"
    }
    if experiments:
        meta["experiments"] = experiments
    return meta


def _experiment_meta(scenario: Scenario) -> dict[str, Any]:
    from evals.experiment import experiment_meta

    return experiment_meta(scenario)


def grade(
    scenario: Scenario, locale: str, outcome: SampleOutcome
) -> tuple[dict[str, Any], dict[str, Any]]:
    trace = collect(outcome.workspace)
    ctx = CheckContext(trace=trace, started_at=outcome.started_at)
    checks = [
        run_check(check.name, check.type, ctx, scenario.check_params_for(check, locale))
        for check in scenario.checks
    ]
    passed = outcome.status == "completed" and all(check.passed for check in checks)
    result = {
        "scenario": scenario.id,
        "scenario_version": scenario.version,
        "locale": locale,
        "arm": scenario.arm,
        "kind": scenario.kind,
        "status": outcome.status,
        "status_detail": outcome.detail,
        "passed": passed,
        "checks": [check.to_dict() for check in checks],
        "started_at": outcome.started_at.isoformat(),
        "real_seconds": outcome.real_seconds,
        "llm_calls": trace.llm_calls,
        "input_tokens": trace.input_tokens,
        "output_tokens": trace.output_tokens,
        "system_prompt_hash": trace.system_prompt_hash,
    }
    return result, trace.to_dict()


@dataclass
class _Halt:
    status: str = ""
    detail: str = ""
    skipped: int = 0


async def _run_one(
    scenario: Scenario,
    locale: str,
    index: int,
    run_dir: Path,
    options: RunOptions,
    semaphore: asyncio.Semaphore,
    halt: _Halt,
) -> dict[str, Any] | None:
    async with semaphore:
        if halt.status:
            halt.skipped += 1
            return None
        sample_dir = run_dir / "samples" / scenario.id
        if scenario.arm:
            sample_dir = sample_dir / scenario.arm
        sample_dir = sample_dir / locale / f"{index:02d}"
        sample_dir.mkdir(parents=True)
        outcome = await run_sample(scenario, locale, options.target, sample_dir)
    if outcome.status in UNOBSERVED_STATUSES and not halt.status:
        halt.status, halt.detail = outcome.status, outcome.detail
    result, trace = grade(scenario, locale, outcome)
    (sample_dir / "trace.json").write_text(
        json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (sample_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    label = f"{scenario.id}/{scenario.arm}" if scenario.arm else scenario.id
    if scenario.kind == "experiment" and scenario.contrast is not None:
        hit = next(
            (item for item in result["checks"] if item["name"] == scenario.contrast.check),
            None,
        )
        mark = "-" if hit is None else ("yes" if hit["passed"] else "no")
        print(
            f"[{result['status']}] {label} {locale} #{index} contrast={mark}",
            flush=True,
        )
    else:
        mark = "PASS" if result["passed"] else "FAIL"
        print(f"[{mark}] {label} {locale} #{index} ({result['status']})", flush=True)
    return result


async def run(scenarios: list[Scenario], options: RunOptions) -> Path:
    sha = _git("rev-parse", "--short", "HEAD") or "nogit"
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{sha}"
    run_dir = options.results_dir / run_id
    run_dir.mkdir(parents=True)
    meta = run_metadata(run_id, scenarios, options)
    (run_dir / "run.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")

    semaphore = asyncio.Semaphore(max(1, options.jobs))
    halt = _Halt()
    jobs = []
    for scenario in scenarios:
        locales = [
            loc for loc in scenario.locales if not options.locales or loc in options.locales
        ]
        for locale in locales:
            for index in range(options.samples or scenario.samples):
                jobs.append(_run_one(scenario, locale, index, run_dir, options, semaphore, halt))
    await asyncio.gather(*jobs)

    meta["finished_at"] = datetime.now(UTC).isoformat()
    if halt.status:
        meta["halted"] = {"status": halt.status, "detail": halt.detail, "skipped": halt.skipped}
    (run_dir / "run.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    return run_dir
