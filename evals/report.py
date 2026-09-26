"""Aggregate sample results into a summary and compare against a baseline run."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from evals.stats import intervals_disjoint, wilson_interval

# The model could not be reached, so the sample says nothing about her. These
# samples are reported but left out of the pass rate, and they halt the run.
UNOBSERVED_STATUSES = frozenset({"provider_error", "setup_mode"})


def load_samples(run_dir: Path) -> list[dict[str, Any]]:
    samples = []
    for path in sorted(run_dir.glob("samples/*/*/*/result.json")):
        samples.append(json.loads(path.read_text(encoding="utf-8")))
    return samples


def summarize(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        groups[(sample["scenario"], sample["locale"])].append(sample)
    rows = []
    for (scenario, locale), group in sorted(groups.items()):
        items = [item for item in group if item["status"] not in UNOBSERVED_STATUSES]
        unobserved: dict[str, int] = defaultdict(int)
        for item in group:
            if item["status"] in UNOBSERVED_STATUSES:
                unobserved[item["status"]] += 1
        total = len(items)
        passes = sum(1 for item in items if item["passed"])
        low, high = wilson_interval(passes, total)
        failures: dict[str, int] = defaultdict(int)
        for item in items:
            for check in item["checks"]:
                if not check["passed"]:
                    failures[check["name"]] += 1
            if item["status"] != "completed":
                failures[f"status:{item['status']}"] += 1
        rows.append(
            {
                "scenario": scenario,
                "locale": locale,
                "samples": total,
                "passes": passes,
                "pass_rate": passes / total if total else 0.0,
                "ci95": [round(low, 3), round(high, 3)],
                "pass_all": bool(total) and passes == total,
                "mean_llm_calls": _mean(items, "llm_calls"),
                "mean_real_seconds": _mean(items, "real_seconds"),
                "failures": dict(sorted(failures.items())),
                "unobserved": dict(sorted(unobserved.items())),
            }
        )
    return rows


def _mean(items: list[dict[str, Any]], key: str) -> float:
    return round(sum(item[key] for item in items) / len(items), 1) if items else 0.0


def compare(rows: list[dict[str, Any]], baseline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {(row["scenario"], row["locale"]): row for row in baseline}
    comparisons = []
    for row in rows:
        base = by_key.get((row["scenario"], row["locale"]))
        if base is None or not base["samples"] or not row["samples"]:
            continue
        delta = row["pass_rate"] - base["pass_rate"]
        disjoint = intervals_disjoint(tuple(row["ci95"]), tuple(base["ci95"]))
        comparisons.append(
            {
                "scenario": row["scenario"],
                "locale": row["locale"],
                "baseline_pass_rate": base["pass_rate"],
                "pass_rate": row["pass_rate"],
                "delta": round(delta, 3),
                "regression": disjoint and delta < 0,
                "improvement": disjoint and delta > 0,
            }
        )
    return comparisons


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def render_markdown(
    meta: dict[str, Any], rows: list[dict[str, Any]], comparisons: list[dict[str, Any]]
) -> str:
    lines = [
        f"# Care run {meta.get('run_id', '')}",
        "",
        f"- target: `{meta.get('provider')}` / `{meta.get('model')}`",
        f"- git: `{meta.get('git_sha', '')[:12]}`" + (" (dirty)" if meta.get("git_dirty") else ""),
        f"- coworker: `{meta.get('coworker_version', '')}`",
    ]
    halted = meta.get("halted")
    if isinstance(halted, dict):
        lines.append(
            f"- **halted** (`{halted.get('status')}`), {halted.get('skipped', 0)} samples "
            f"not started: {halted.get('detail', '')}"
        )
    lines += [
        "",
        "| scenario | locale | pass | 95% CI | all passed | LLM calls | seconds | failures "
        "| unobserved |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        failures = ", ".join(f"{name}×{count}" for name, count in row["failures"].items())
        unobserved = ", ".join(
            f"{name}×{count}" for name, count in row.get("unobserved", {}).items()
        )
        lines.append(
            f"| {row['scenario']} | {row['locale']} | {row['passes']}/{row['samples']} "
            f"| {_pct(row['ci95'][0])}–{_pct(row['ci95'][1])} "
            f"| {'yes' if row['pass_all'] else 'no'} | {row['mean_llm_calls']} "
            f"| {row['mean_real_seconds']} | {failures or '-'} | {unobserved or '-'} |"
        )
    if comparisons:
        lines += [
            "",
            "## Compared with baseline",
            "",
            "| scenario | locale | baseline | now | delta | flag |",
            "|---|---|---|---|---|---|",
        ]
        for item in comparisons:
            flag = "regression" if item["regression"] else (
                "improvement" if item["improvement"] else "-"
            )
            lines.append(
                f"| {item['scenario']} | {item['locale']} | {_pct(item['baseline_pass_rate'])} "
                f"| {_pct(item['pass_rate'])} | {item['delta']:+.2f} | {flag} |"
            )
    return "\n".join(lines) + "\n"


def write_report(run_dir: Path, baseline_dir: Path | None = None) -> str:
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    rows = summarize(load_samples(run_dir))
    comparisons: list[dict[str, Any]] = []
    if baseline_dir is not None:
        comparisons = compare(rows, summarize(load_samples(baseline_dir)))
    summary = {"run": meta, "rows": rows, "comparisons": comparisons}
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown = render_markdown(meta, rows, comparisons)
    (run_dir / "summary.md").write_text(markdown, encoding="utf-8")
    return markdown
