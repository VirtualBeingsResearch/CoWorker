"""Pre-registered experiments: the same script on control and treatment arms."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any

from evals.report import UNOBSERVED_STATUSES
from evals.scenario import Scenario

SUPPORTED = "supported"
FALSIFIED = "falsified"
INCONCLUSIVE = "inconclusive"
UNOBSERVED = "unobserved"


def materialize(scenario: Scenario) -> list[Scenario]:
    """Expand one experiment into one runnable scenario per arm."""
    if scenario.kind != "experiment":
        return [scenario]
    return [replace(scenario, state=arm.state, arm=arm.id) for arm in scenario.arms]


def experiment_meta(scenario: Scenario) -> dict[str, Any]:
    contrast = scenario.contrast
    assert contrast is not None
    return {
        "hypothesis": scenario.preregistration.hypothesis.strip(),
        "falsified_if": scenario.preregistration.falsified_if.strip(),
        "expected_side_effects": list(scenario.preregistration.expected_side_effects),
        "contrast": {
            "check": contrast.check,
            "present": list(contrast.present),
            "absent": list(contrast.absent),
        },
        "arms": [{"id": arm.id, "state": arm.state} for arm in scenario.arms],
    }


def _check(sample: dict[str, Any], name: str) -> dict[str, Any] | None:
    for item in sample.get("checks") or []:
        if item.get("name") == name:
            return item
    return None


def _arm_contrast(samples: list[dict[str, Any]], name: str) -> bool | None:
    """True if every sample passed the named check, False if every sample failed, else None."""
    hits: list[bool] = []
    for sample in samples:
        item = _check(sample, name)
        if item is None:
            return None
        hits.append(bool(item.get("passed")))
    if not hits:
        return None
    if all(hits):
        return True
    if not any(hits):
        return False
    return None


def _arm_status(samples: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("status") or "") for item in samples}
    if len(statuses) == 1:
        return statuses.pop()
    return "mixed"


def conclude_locale(
    scenario_id: str,
    locale: str,
    spec: dict[str, Any],
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    """Decide supported / falsified / inconclusive / unobserved for one locale."""
    contrast = spec["contrast"]
    check_name = str(contrast["check"])
    arm_ids = [arm["id"] for arm in spec["arms"]]
    by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        arm_id = str(sample.get("arm") or "")
        if arm_id in arm_ids:
            by_arm[arm_id].append(sample)
    missing = [arm_id for arm_id in arm_ids if not by_arm[arm_id]]
    if missing:
        return {
            "scenario": scenario_id,
            "locale": locale,
            "conclusion": INCONCLUSIVE,
            "detail": f"missing arm samples: {missing}",
            "arms": {},
        }

    unobserved = [
        arm_id
        for arm_id in arm_ids
        if any(item.get("status") in UNOBSERVED_STATUSES for item in by_arm[arm_id])
    ]
    if unobserved:
        return {
            "scenario": scenario_id,
            "locale": locale,
            "conclusion": UNOBSERVED,
            "detail": f"unobserved arms: {unobserved}",
            "arms": {
                arm_id: {
                    "status": _arm_status(by_arm[arm_id]),
                    "contrast_passed": None,
                }
                for arm_id in arm_ids
            },
        }

    incomplete = [
        arm_id
        for arm_id in arm_ids
        if any(item.get("status") != "completed" for item in by_arm[arm_id])
    ]
    contrast_hits = {arm_id: _arm_contrast(by_arm[arm_id], check_name) for arm_id in arm_ids}
    arms = {
        arm_id: {
            "status": _arm_status(by_arm[arm_id]),
            "contrast_passed": contrast_hits[arm_id],
        }
        for arm_id in arm_ids
    }

    if incomplete:
        return {
            "scenario": scenario_id,
            "locale": locale,
            "conclusion": INCONCLUSIVE,
            "detail": f"arms did not complete: {incomplete}",
            "arms": arms,
        }
    if any(value is None for value in contrast_hits.values()):
        return {
            "scenario": scenario_id,
            "locale": locale,
            "conclusion": INCONCLUSIVE,
            "detail": f"contrast check {check_name!r} missing or mixed on an arm",
            "arms": arms,
        }

    present = list(contrast["present"])
    absent = list(contrast["absent"])
    present_hits = {arm_id: contrast_hits[arm_id] for arm_id in present}
    absent_hits = {arm_id: contrast_hits[arm_id] for arm_id in absent}
    if all(present_hits.values()) and not any(absent_hits.values()):
        return {
            "scenario": scenario_id,
            "locale": locale,
            "conclusion": SUPPORTED,
            "detail": f"{check_name} passed on {present} and failed on {absent}",
            "arms": arms,
        }
    return {
        "scenario": scenario_id,
        "locale": locale,
        "conclusion": FALSIFIED,
        "detail": f"{check_name} present={present_hits} absent={absent_hits}",
        "arms": arms,
    }


def conclude_run(
    specs: dict[str, dict[str, Any]], samples: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        groups[(sample["scenario"], sample["locale"])].append(sample)
    rows = []
    for (scenario_id, locale), group in sorted(groups.items()):
        spec = specs.get(scenario_id)
        if spec is None:
            continue
        rows.append(conclude_locale(scenario_id, locale, spec, group))
    return rows


def render_experiment_markdown(
    meta: dict[str, Any], conclusions: list[dict[str, Any]]
) -> str:
    specs = meta.get("experiments") or {}
    lines = [
        f"# Experiment run {meta.get('run_id', '')}",
        "",
        f"- target: `{meta.get('provider')}` / `{meta.get('model')}`",
        f"- git: `{meta.get('git_sha', '')[:12]}`" + (" (dirty)" if meta.get("git_dirty") else ""),
        f"- coworker: `{meta.get('coworker_version', '')}`",
        "",
        "Conclusions are control contrasts, not pass rates.",
    ]
    halted = meta.get("halted")
    if isinstance(halted, dict):
        lines.append(
            f"- **halted** (`{halted.get('status')}`), {halted.get('skipped', 0)} samples "
            f"not started: {halted.get('detail', '')}"
        )
    for row in conclusions:
        spec = specs.get(row["scenario"]) or {}
        lines += ["", f"## {row['scenario']} ({row['locale']})", ""]
        if spec.get("hypothesis"):
            lines.append(f"- hypothesis: {spec['hypothesis']}")
        if spec.get("falsified_if"):
            lines.append(f"- falsified if: {spec['falsified_if']}")
        contrast = spec.get("contrast") or {}
        if contrast.get("check"):
            lines.append(f"- contrast: `{contrast['check']}`")
        lines.append(f"- **conclusion: {row['conclusion']}** — {row.get('detail', '')}")
        lines += [
            "",
            "| arm | status | contrast |",
            "|---|---|---|",
        ]
        for arm_id, info in (row.get("arms") or {}).items():
            hit = info.get("contrast_passed")
            mark = "-" if hit is None else ("yes" if hit else "no")
            lines.append(f"| {arm_id} | {info.get('status', '')} | {mark} |")
    return "\n".join(lines) + "\n"
