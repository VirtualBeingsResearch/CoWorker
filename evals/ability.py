"""Ability suites: authored items, raw-model vs newborn Coworker, official-style scoring."""

from __future__ import annotations

import ast
import hashlib
import json
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any, Literal

import yaml

from evals.driver import run_sample
from evals.extract import extract_choice, extract_code, extract_exact
from evals.judge import ensure_different_vendor, format_evidence, judge_text
from evals.llm import complete_text
from evals.runner import DEFAULT_RESULTS_DIR, _coworker_version, _git
from evals.scenario import (
    SETTLE,
    CheckSpec,
    Guard,
    Preregistration,
    Scenario,
    ScenarioError,
    Step,
    localize,
)
from evals.stats import wilson_interval
from evals.trace import collect
from evals.user import SimulatedUser
from evals.workspace import REPO_ROOT, ModelTarget

DEFAULT_ABILITIES = REPO_ROOT / "evals" / "abilities"
SUPPORTED_SCORING = ("choice", "exact", "code", "judge")
Book = Literal["closed", "open"]


@dataclass(frozen=True)
class AbilityItem:
    id: str
    prompt: Any
    answer: str = ""
    rubric: str = ""
    files: dict[str, Any] = field(default_factory=dict)
    expected_stdout: str = ""
    code_entry: str = ""


@dataclass(frozen=True)
class AbilitySuite:
    id: str
    version: int
    domain: str
    locales: tuple[str, ...]
    scoring: str
    book: Book
    items: tuple[AbilityItem, ...]
    source: Path
    content_hash: str
    user_goal: str = ""
    max_user_turns: int = 1

    def prompt_for(self, item: AbilityItem, locale: str) -> str:
        return str(localize(item.prompt, locale)).strip()

    def files_for(self, item: AbilityItem, locale: str) -> dict[str, str]:
        return {path: str(localize(content, locale)) for path, content in item.files.items()}


def load_suite(path: str | Path) -> AbilitySuite:
    source = Path(path)
    raw_text = source.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text)
    if not isinstance(data, dict):
        raise ScenarioError(f"{source}: suite must be a mapping")
    scoring = str(data.get("scoring") or "choice")
    if scoring not in SUPPORTED_SCORING:
        raise ScenarioError(f"{source}: scoring must be one of {SUPPORTED_SCORING}")
    book = str(data.get("book") or "closed")
    if book not in {"closed", "open"}:
        raise ScenarioError(f"{source}: book must be closed or open")
    items_raw = data.get("items") or []
    if not isinstance(items_raw, list) or not items_raw:
        raise ScenarioError(f"{source}: items must be a non-empty list")
    items = []
    for index, raw in enumerate(items_raw):
        if not isinstance(raw, dict) or not raw.get("id"):
            raise ScenarioError(f"{source}: items[{index}] needs an id")
        items.append(
            AbilityItem(
                id=str(raw["id"]),
                prompt=raw.get("prompt") or raw.get("opening") or "",
                answer=str(raw.get("answer") or ""),
                rubric=str(raw.get("rubric") or ""),
                files=dict(raw.get("files") or {}),
                expected_stdout=str(raw.get("expected_stdout") or ""),
                code_entry=str(raw.get("code") or ""),
            )
        )
    locales = tuple(str(x) for x in data.get("locales") or ("zh-CN", "en"))
    suite = AbilitySuite(
        id=str(data.get("id") or source.stem),
        version=int(data.get("version") or 1),
        domain=str(data.get("domain") or "knowledge"),
        locales=locales,
        scoring=scoring,
        book=book,  # type: ignore[arg-type]
        items=tuple(items),
        source=source,
        content_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:16],
        user_goal=str(data.get("user_goal") or ""),
        max_user_turns=int(data.get("max_user_turns") or 1),
    )
    for locale in suite.locales:
        for item in suite.items:
            if not suite.prompt_for(item, locale):
                raise ScenarioError(f"{source}: empty {locale} prompt for {item.id}")
    return suite


def discover_suites(paths: list[str | Path]) -> list[AbilitySuite]:
    files: list[Path] = []
    for path in map(Path, paths):
        if path.is_dir():
            files.extend(sorted(path.rglob("*.yaml")))
        elif path.is_file():
            files.append(path)
        else:
            raise ScenarioError(f"ability path not found: {path}")
    suites = [load_suite(file) for file in files]
    seen: dict[str, Path] = {}
    for suite in suites:
        if suite.id in seen:
            raise ScenarioError(f"duplicate suite id {suite.id!r}")
        seen[suite.id] = suite.source
    return suites


def item_scenario(suite: AbilitySuite, item: AbilityItem, locale: str) -> Scenario:
    files = suite.files_for(item, locale)
    config: dict[str, dict[str, Any]] = {}
    if suite.book == "closed":
        config["agent"] = {"browser_enabled": False}
    checks: list[CheckSpec] = [CheckSpec("replied", {"participant": "alice"}, "replied")]
    return Scenario(
        id=f"{suite.id}.{item.id}",
        version=suite.version,
        kind="care",
        preregistration=Preregistration(hypothesis=suite.domain),
        locales=(locale,),
        samples=1,
        guard=Guard(max_llm_calls=40, max_seconds=360, settle_seconds=3.0),
        config=config,
        files=files,
        script=(Step(participant="alice", say=suite.prompt_for(item, locale), after=SETTLE),),
        checks=tuple(checks),
        source=suite.source,
        content_hash=suite.content_hash,
    )


def score_text(suite: AbilitySuite, item: AbilityItem, text: str) -> dict[str, Any]:
    if suite.scoring == "choice":
        extracted = extract_choice(text)
        passed = not extracted.failed and extracted.value == item.answer.upper()
        return {
            "passed": passed,
            "extract_failed": extracted.failed,
            "extracted": extracted.value,
            "detail": extracted.detail,
        }
    if suite.scoring == "exact":
        extracted = extract_exact(text)
        passed = not extracted.failed and extracted.value == item.answer
        return {
            "passed": passed,
            "extract_failed": extracted.failed,
            "extracted": extracted.value,
            "detail": extracted.detail,
        }
    if suite.scoring == "code":
        if item.expected_stdout and item.expected_stdout.strip() in text:
            return {
                "passed": True,
                "extract_failed": False,
                "extracted": item.expected_stdout.strip(),
                "detail": "expected stdout appeared in the reply",
            }
        extracted = extract_code(text)
        if item.code_entry.strip():
            source = item.code_entry
        else:
            source = extracted.value
        ok, detail = _exec_code(source, item.expected_stdout)
        return {
            "passed": ok,
            "extract_failed": extracted.failed and not item.code_entry,
            "extracted": source[:500],
            "detail": detail,
        }
    return {
        "passed": False,
        "extract_failed": False,
        "extracted": text[:200],
        "detail": "judge scoring is applied after the run",
    }


def _exec_code(source: str, expected_stdout: str) -> tuple[bool, str]:
    if not source.strip():
        return False, "no code"
    namespace: dict[str, Any] = {}
    try:
        tree = ast.parse(source)
        exec(compile(tree, "<ability>", "exec"), namespace, namespace)  # noqa: S102
    except Exception as error:
        return False, f"{type(error).__name__}: {error}"
    if expected_stdout and "main" in namespace:
        buffer = StringIO()
        with redirect_stdout(buffer):
            namespace["main"]()
        got = buffer.getvalue().strip()
        if got == expected_stdout.strip():
            return True, "stdout matched"
        return False, f"stdout {got!r} != {expected_stdout.strip()!r}"
    if expected_stdout:
        return True, "code parsed; stdout check skipped (no main())"
    return True, "code parsed"


def _reply_text(workspace: Path) -> str:
    trace = collect(workspace)
    return "\n".join(message.message for message in trace.messages_to("alice"))


@dataclass(frozen=True)
class AbilityOptions:
    target: ModelTarget
    judge: ModelTarget | None = None
    user: ModelTarget | None = None
    modes: tuple[str, ...] = ("raw", "newborn")
    locales: tuple[str, ...] | None = None
    results_dir: Path = DEFAULT_RESULTS_DIR


async def _run_raw(suite: AbilitySuite, item: AbilityItem, locale: str, target: ModelTarget) -> dict:
    prompt = suite.prompt_for(item, locale)
    text = await complete_text(target, prompt)
    return {"mode": "raw", "reply": text, **score_text(suite, item, text)}


async def _score_judge(
    suite: AbilitySuite,
    item: AbilityItem,
    locale: str,
    evidence: dict[str, object],
    judge: ModelTarget,
) -> dict[str, Any]:
    verdict = await judge_text(
        judge, rubric=item.rubric or suite.user_goal, evidence=format_evidence(evidence), locale=locale
    )
    return {
        "passed": verdict.passed,
        "extract_failed": False,
        "extracted": "",
        "detail": verdict.reason,
    }


async def _run_newborn(
    suite: AbilitySuite,
    item: AbilityItem,
    locale: str,
    options: AbilityOptions,
    sample_dir: Path,
) -> dict[str, Any]:
    scenario = item_scenario(suite, item, locale)
    if suite.scoring == "judge" and suite.max_user_turns > 1:
        from evals.driver import run_dialogue

        user = SimulatedUser(
            options.user or options.judge or options.target,
            goal=suite.user_goal or item.rubric,
            locale=locale,
            max_turns=suite.max_user_turns,
        )
        outcome = await run_dialogue(scenario, locale, options.target, sample_dir, user)
    else:
        outcome = await run_sample(scenario, locale, options.target, sample_dir)
    reply = _reply_text(outcome.workspace) if outcome.workspace.exists() else ""
    if suite.scoring == "judge":
        if options.judge is None:
            raise RuntimeError("judge scoring needs --judge-provider / --judge-model")
        scored = await _score_judge(
            suite,
            item,
            locale,
            {"reply": reply, "status": outcome.status, "goal": suite.user_goal},
            options.judge,
        )
    else:
        scored = score_text(suite, item, reply)
    return {
        "mode": "newborn",
        "status": outcome.status,
        "status_detail": outcome.detail,
        "reply": reply,
        "real_seconds": outcome.real_seconds,
        **scored,
    }


async def run_abilities(suites: list[AbilitySuite], options: AbilityOptions) -> Path:
    if options.judge is not None:
        ensure_different_vendor(options.target, options.judge)
    sha = _git("rev-parse", "--short", "HEAD") or "nogit"
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{sha}-ability"
    run_dir = options.results_dir / run_id
    run_dir.mkdir(parents=True)
    meta = {
        "run_id": run_id,
        "kind": "ability",
        "started_at": datetime.now(UTC).isoformat(),
        "git_sha": _git("rev-parse", "HEAD"),
        "coworker_version": _coworker_version(),
        "provider": options.target.provider,
        "model": options.target.model,
        "modes": list(options.modes),
        "suites": {suite.id: {"version": suite.version, "hash": suite.content_hash} for suite in suites},
    }
    (run_dir / "run.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")

    rows: list[dict[str, Any]] = []
    for suite in suites:
        locales = [loc for loc in suite.locales if not options.locales or loc in options.locales]
        for locale in locales:
            for item in suite.items:
                record: dict[str, Any] = {
                    "suite": suite.id,
                    "item": item.id,
                    "locale": locale,
                    "domain": suite.domain,
                    "scoring": suite.scoring,
                    "book": suite.book,
                    "modes": {},
                }
                if "raw" in options.modes:
                    raw = await _run_raw(suite, item, locale, options.target)
                    if suite.scoring == "judge":
                        if options.judge is None:
                            raise RuntimeError("judge scoring needs a judge target")
                        raw.update(
                            await _score_judge(
                                suite,
                                item,
                                locale,
                                {"reply": raw["reply"], "goal": suite.user_goal},
                                options.judge,
                            )
                        )
                    record["modes"]["raw"] = raw
                    mark = "PASS" if raw["passed"] else "FAIL"
                    print(f"[{mark}] raw {suite.id} {item.id} {locale}", flush=True)
                if "newborn" in options.modes:
                    sample_dir = run_dir / "samples" / suite.id / locale / item.id / "newborn"
                    sample_dir.mkdir(parents=True)
                    newborn = await _run_newborn(suite, item, locale, options, sample_dir)
                    record["modes"]["newborn"] = newborn
                    mark = "PASS" if newborn["passed"] else "FAIL"
                    print(f"[{mark}] newborn {suite.id} {item.id} {locale}", flush=True)
                rows.append(record)
                _write_item(run_dir, record)

    summary = _summarize(rows)
    meta["finished_at"] = datetime.now(UTC).isoformat()
    (run_dir / "run.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    payload = {"run": meta, "rows": summary, "items": rows}
    (run_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "summary.md").write_text(_render(meta, summary), encoding="utf-8")
    return run_dir


def _write_item(run_dir: Path, record: dict[str, Any]) -> None:
    path = run_dir / "items" / record["suite"] / record["locale"] / f"{record['item']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def _summarize(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in items:
        for mode, result in item["modes"].items():
            groups.setdefault((item["suite"], item["locale"], mode), []).append(result)
    rows = []
    for (suite, locale, mode), results in sorted(groups.items()):
        graded = [item for item in results if not item.get("extract_failed")]
        extract_failed = len(results) - len(graded)
        passes = sum(1 for item in graded if item.get("passed"))
        total = len(graded)
        low, high = wilson_interval(passes, total)
        rows.append(
            {
                "suite": suite,
                "locale": locale,
                "mode": mode,
                "passes": passes,
                "samples": total,
                "extract_failed": extract_failed,
                "pass_rate": passes / total if total else 0.0,
                "ci95": [round(low, 3), round(high, 3)],
            }
        )
    return rows


def _render(meta: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        f"# Ability run {meta.get('run_id', '')}",
        "",
        f"- target: `{meta.get('provider')}` / `{meta.get('model')}`",
        f"- modes: {', '.join(meta.get('modes') or [])}",
        "",
        "| suite | locale | mode | pass | 95% CI | extract failed |",
        "|---|---|---|---|---|---|",
    ]
    by_key: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for row in rows:
        by_key.setdefault((row["suite"], row["locale"]), {})[row["mode"]] = row
        lines.append(
            f"| {row['suite']} | {row['locale']} | {row['mode']} | "
            f"{row['passes']}/{row['samples']} | "
            f"{row['ci95'][0] * 100:.0f}%–{row['ci95'][1] * 100:.0f}% | "
            f"{row['extract_failed']} |"
        )
    deltas = []
    for (suite, locale), modes in sorted(by_key.items()):
        if "raw" in modes and "newborn" in modes and modes["raw"]["samples"] and modes["newborn"]["samples"]:
            delta = modes["newborn"]["pass_rate"] - modes["raw"]["pass_rate"]
            deltas.append(
                f"| {suite} | {locale} | {modes['raw']['pass_rate'] * 100:.0f}% | "
                f"{modes['newborn']['pass_rate'] * 100:.0f}% | {delta:+.2f} |"
            )
    if deltas:
        lines += [
            "",
            "## Organ vs her",
            "",
            "| suite | locale | raw | newborn | delta |",
            "|---|---|---|---|---|",
            *deltas,
        ]
    return "\n".join(lines) + "\n"
