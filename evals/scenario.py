"""Scenario definitions: loading, validation, and locale resolution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from evals.duration import parse_duration

SUPPORTED_LOCALES = ("zh-CN", "en")
SUPPORTED_KINDS = ("care", "experiment")
RESERVED_KINDS = ("ability", "acquaintance")
CONFIG_SECTIONS = {
    "agent": "AGENT__",
    "memory": "MEMORY__",
    "llm": "LLM__",
    "i18n": "I18N__",
}
SETTLE = "settle"


class ScenarioError(ValueError):
    """A scenario file is malformed or uses an unsupported feature."""


def localize(value: Any, locale: str) -> Any:
    """Resolve ``{zh-CN: ..., en: ...}`` mappings recursively for one locale."""
    if isinstance(value, dict):
        if value and set(value) <= set(SUPPORTED_LOCALES):
            if locale not in value:
                raise ScenarioError(f"missing {locale!r} text in {value!r}")
            return localize(value[locale], locale)
        return {key: localize(item, locale) for key, item in value.items()}
    if isinstance(value, list):
        return [localize(item, locale) for item in value]
    return value


@dataclass(frozen=True)
class Preregistration:
    hypothesis: str
    expected_side_effects: tuple[str, ...] = ()
    falsified_if: str = ""


@dataclass(frozen=True)
class Guard:
    max_llm_calls: int = 200
    max_seconds: float = 900.0
    settle_seconds: float = 4.0


@dataclass(frozen=True)
class ClockSpec:
    start: str = ""
    jitter: str = ""
    horizon: str = ""


@dataclass(frozen=True)
class Arm:
    id: str
    state: str


@dataclass(frozen=True)
class Contrast:
    """Named check that should pass on ``present`` arms and fail on ``absent`` arms."""

    check: str
    present: tuple[str, ...]
    absent: tuple[str, ...]


@dataclass(frozen=True)
class Step:
    participant: str
    say: Any
    after: str | float = SETTLE
    at: str = ""
    at_seconds: float | None = None
    action: str = ""


@dataclass(frozen=True)
class CheckSpec:
    type: str
    params: dict[str, Any] = field(default_factory=dict)
    label: str = ""

    @property
    def name(self) -> str:
        return self.label or self.type


@dataclass(frozen=True)
class Scenario:
    id: str
    version: int
    kind: str
    preregistration: Preregistration
    locales: tuple[str, ...]
    samples: int
    guard: Guard
    config: dict[str, dict[str, Any]]
    files: dict[str, Any]
    script: tuple[Step, ...]
    checks: tuple[CheckSpec, ...]
    source: Path
    content_hash: str
    state: str = "newborn"
    clock: ClockSpec = field(default_factory=ClockSpec)
    arm: str = ""
    arms: tuple[Arm, ...] = ()
    contrast: Contrast | None = None

    @property
    def clock_start(self) -> str:
        return self.clock.start

    @property
    def uses_timeline(self) -> bool:
        return any(step.at_seconds is not None for step in self.script)

    def env_overrides(self) -> dict[str, str]:
        """Translate ``config`` sections into Coworker environment variables."""
        env: dict[str, str] = {}
        for section, values in self.config.items():
            prefix = CONFIG_SECTIONS[section]
            for key, value in values.items():
                env[f"{prefix}{key.upper()}"] = _env_value(value)
        return env

    def files_for(self, locale: str) -> dict[str, str]:
        return {path: str(localize(content, locale)) for path, content in self.files.items()}

    def message_for(self, step: Step, locale: str) -> str:
        return str(localize(step.say, locale))

    def check_params_for(self, check: CheckSpec, locale: str) -> dict[str, Any]:
        resolved = localize(check.params, locale)
        assert isinstance(resolved, dict)
        return resolved


def _env_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _require(data: dict[str, Any], key: str, source: Path) -> Any:
    if key not in data:
        raise ScenarioError(f"{source}: missing required field {key!r}")
    return data[key]


def _parse_clock(raw: Any, source: Path) -> ClockSpec:
    if not raw:
        return ClockSpec()
    if isinstance(raw, str):
        spec = ClockSpec(start=raw)
    elif not isinstance(raw, dict):
        raise ScenarioError(f"{source}: clock must be a mapping or an ISO timestamp")
    else:
        spec = ClockSpec(
            start=str(raw.get("start") or ""),
            jitter=str(raw.get("jitter") or ""),
            horizon=str(raw.get("horizon") or ""),
        )
    for field_name, value in (("jitter", spec.jitter), ("horizon", spec.horizon)):
        if not value:
            continue
        try:
            parse_duration(value)
        except ValueError as error:
            raise ScenarioError(f"{source}: clock.{field_name} is not a duration") from error
    if spec.start:
        try:
            origin = datetime.fromisoformat(spec.start)
        except ValueError as error:
            raise ScenarioError(f"{source}: clock.start is not an ISO timestamp") from error
        if origin.tzinfo is None:
            raise ScenarioError(f"{source}: clock.start must include a timezone")
    return spec


def _parse_step(raw: Any, index: int, source: Path) -> Step:
    if not isinstance(raw, dict):
        raise ScenarioError(f"{source}: script[{index}] must be a mapping")
    if "at" in raw and "after" in raw:
        raise ScenarioError(f"{source}: script[{index}] cannot mix at: and after:")
    action = str(raw.get("action") or "").strip()
    if action and action != "restart":
        raise ScenarioError(
            f"{source}: script[{index}].action must be 'restart', not {action!r}"
        )
    at_raw = raw.get("at")
    at = str(at_raw).strip() if at_raw is not None and str(at_raw).strip() else ""
    at_seconds: float | None = None
    if at:
        try:
            at_seconds = parse_duration(at)
        except ValueError as error:
            raise ScenarioError(
                f"{source}: script[{index}].at is not a duration"
            ) from error
        if at_seconds < 0:
            raise ScenarioError(f"{source}: script[{index}].at must not be negative")

    if action == "restart":
        if at_seconds is None:
            raise ScenarioError(f"{source}: script[{index}] restart needs at:")
        extra = set(raw) - {"at", "action"}
        if extra:
            raise ScenarioError(
                f"{source}: script[{index}] restart cannot include {sorted(extra)}"
            )
        return Step(
            participant="",
            say="",
            at=at,
            at_seconds=at_seconds,
            action="restart",
        )

    participant = str(_require(raw, "from", source)).strip()
    if not participant:
        raise ScenarioError(f"{source}: script[{index}].from must not be empty")
    after = raw.get("after", SETTLE)
    if after != SETTLE:
        try:
            after = float(after)
        except (TypeError, ValueError) as error:
            raise ScenarioError(
                f"{source}: script[{index}].after must be 'settle' or seconds"
            ) from error
        if after < 0:
            raise ScenarioError(f"{source}: script[{index}].after must not be negative")
    return Step(
        participant=participant,
        say=_require(raw, "say", source),
        after=after,
        at=at,
        at_seconds=at_seconds,
    )


def horizon_seconds(scenario: Scenario) -> float:
    """Virtual seconds from clock origin until the sample should stop."""
    if scenario.clock.horizon:
        return parse_duration(scenario.clock.horizon)
    times = [step.at_seconds for step in scenario.script if step.at_seconds is not None]
    return (max(times) if times else 0.0) + 60.0


def _parse_arms(raw: Any, source: Path) -> tuple[Arm, ...]:
    if not isinstance(raw, list) or len(raw) < 2:
        raise ScenarioError(f"{source}: experiment arms must be a list of at least two mappings")
    arms: list[Arm] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ScenarioError(f"{source}: arms[{index}] must be a mapping")
        arm_id = str(item.get("id") or "").strip()
        state = str(item.get("state") or "").strip()
        extra = set(item) - {"id", "state"}
        if extra:
            raise ScenarioError(f"{source}: arms[{index}] cannot include {sorted(extra)}")
        if not arm_id:
            raise ScenarioError(f"{source}: arms[{index}].id must not be empty")
        if arm_id in seen:
            raise ScenarioError(f"{source}: duplicate arm id {arm_id!r}")
        if not state:
            raise ScenarioError(f"{source}: arms[{index}].state must not be empty")
        seen.add(arm_id)
        arms.append(Arm(id=arm_id, state=state))
    return tuple(arms)


def _parse_contrast(
    raw: Any, arms: tuple[Arm, ...], checks: tuple[CheckSpec, ...], source: Path
) -> Contrast:
    if not isinstance(raw, dict):
        raise ScenarioError(f"{source}: experiment contrast must be a mapping")
    extra = set(raw) - {"check", "present", "absent"}
    if extra:
        raise ScenarioError(f"{source}: contrast cannot include {sorted(extra)}")
    check = str(raw.get("check") or "").strip()
    names = {item.name for item in checks}
    if check not in names:
        raise ScenarioError(
            f"{source}: contrast.check {check!r} is not a check name; known: {sorted(names)}"
        )
    present = tuple(str(item).strip() for item in (raw.get("present") or []) if str(item).strip())
    absent = tuple(str(item).strip() for item in (raw.get("absent") or []) if str(item).strip())
    if not present or not absent:
        raise ScenarioError(f"{source}: contrast.present and contrast.absent must both be non-empty")
    overlap = set(present) & set(absent)
    if overlap:
        raise ScenarioError(f"{source}: contrast arm {sorted(overlap)} cannot be both present and absent")
    arm_ids = {arm.id for arm in arms}
    unknown = [item for item in (*present, *absent) if item not in arm_ids]
    if unknown:
        raise ScenarioError(f"{source}: contrast refers to unknown arm {unknown}")
    return Contrast(check=check, present=present, absent=absent)


def _parse_check(raw: Any, index: int, source: Path) -> CheckSpec:
    from evals.graders import GRADERS

    if not isinstance(raw, dict):
        raise ScenarioError(f"{source}: checks[{index}] must be a mapping")
    params = dict(raw)
    check_type = str(params.pop("type", ""))
    if check_type not in GRADERS:
        known = ", ".join(sorted(GRADERS))
        raise ScenarioError(
            f"{source}: checks[{index}] has unknown type {check_type!r}; known: {known}"
        )
    label = str(params.pop("label", ""))
    return CheckSpec(type=check_type, params=params, label=label)


def load_scenario(path: str | Path) -> Scenario:
    source = Path(path)
    raw_text = source.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text)
    if not isinstance(data, dict):
        raise ScenarioError(f"{source}: scenario must be a mapping")

    kind = str(_require(data, "kind", source))
    if kind in RESERVED_KINDS:
        raise ScenarioError(f"{source}: kind {kind!r} is reserved and not implemented yet")
    if kind not in SUPPORTED_KINDS:
        raise ScenarioError(f"{source}: unknown kind {kind!r}")

    prereg_raw = _require(data, "preregistration", source)
    if not isinstance(prereg_raw, dict) or not prereg_raw.get("hypothesis"):
        raise ScenarioError(f"{source}: preregistration.hypothesis is required")
    preregistration = Preregistration(
        hypothesis=str(prereg_raw["hypothesis"]),
        expected_side_effects=tuple(str(x) for x in prereg_raw.get("expected_side_effects", [])),
        falsified_if=str(prereg_raw.get("falsified_if", "")),
    )

    locales = tuple(str(x) for x in data.get("locales", SUPPORTED_LOCALES))
    unknown_locales = set(locales) - set(SUPPORTED_LOCALES)
    if not locales or unknown_locales:
        raise ScenarioError(f"{source}: unsupported locales {sorted(unknown_locales)}")

    config = data.get("config") or {}
    if not isinstance(config, dict) or set(config) - set(CONFIG_SECTIONS):
        raise ScenarioError(
            f"{source}: config sections must be among {sorted(CONFIG_SECTIONS)}"
        )

    guard_raw = data.get("guard") or {}
    try:
        guard = Guard(**guard_raw)
    except TypeError as error:
        raise ScenarioError(f"{source}: invalid guard: {error}") from error

    script_raw = _require(data, "script", source)
    if not isinstance(script_raw, list) or not script_raw:
        raise ScenarioError(f"{source}: script must be a non-empty list")
    checks_raw = _require(data, "checks", source)
    if not isinstance(checks_raw, list) or not checks_raw:
        raise ScenarioError(f"{source}: checks must be a non-empty list")
    checks = tuple(_parse_check(item, i, source) for i, item in enumerate(checks_raw))

    default_samples = 1 if kind == "experiment" else 5
    arms: tuple[Arm, ...] = ()
    contrast: Contrast | None = None
    if kind == "experiment":
        if not preregistration.falsified_if.strip():
            raise ScenarioError(f"{source}: experiment preregistration.falsified_if is required")
        if data.get("state") is not None:
            raise ScenarioError(f"{source}: experiment starting state belongs on arms")
        arms = _parse_arms(data.get("arms"), source)
        contrast = _parse_contrast(data.get("contrast"), arms, checks, source)
    elif data.get("arms") is not None or data.get("contrast") is not None:
        raise ScenarioError(f"{source}: arms and contrast are only valid on kind experiment")

    scenario = Scenario(
        id=str(_require(data, "id", source)),
        version=int(data.get("version", 1)),
        kind=kind,
        preregistration=preregistration,
        locales=locales,
        samples=int(data.get("samples", default_samples)),
        guard=guard,
        config={section: dict(values or {}) for section, values in config.items()},
        files=dict(data.get("files") or {}),
        script=tuple(_parse_step(item, i, source) for i, item in enumerate(script_raw)),
        checks=checks,
        source=source,
        content_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:16],
        state=str(data.get("state") or "newborn"),
        clock=_parse_clock(data.get("clock") or data.get("clock_start") or {}, source),
        arms=arms,
        contrast=contrast,
    )
    at_steps = [step for step in scenario.script if step.at_seconds is not None]
    settle_steps = [step for step in scenario.script if step.at_seconds is None]
    if at_steps and settle_steps:
        raise ScenarioError(f"{source}: mix of at: and after:/settle steps is not allowed")
    if at_steps and not scenario.clock.start:
        raise ScenarioError(f"{source}: at: scripts require clock.start")
    if scenario.clock.horizon and at_steps:
        limit = parse_duration(scenario.clock.horizon)
        late = [step.at for step in at_steps if (step.at_seconds or 0) > limit]
        if late:
            raise ScenarioError(f"{source}: at: {late} is after clock.horizon")
    for locale in scenario.locales:
        for step in scenario.script:
            if step.action:
                continue
            if not scenario.message_for(step, locale).strip():
                raise ScenarioError(f"{source}: empty {locale} message for {step.participant}")
        scenario.files_for(locale)
        for check in scenario.checks:
            scenario.check_params_for(check, locale)
    return scenario


def discover(paths: list[str | Path]) -> list[Scenario]:
    """Load every ``*.yaml`` scenario under the given files or directories."""
    files: list[Path] = []
    for path in map(Path, paths):
        if path.is_dir():
            files.extend(sorted(path.rglob("*.yaml")))
        elif path.is_file():
            files.append(path)
        else:
            raise ScenarioError(f"scenario path not found: {path}")
    scenarios = [load_scenario(file) for file in files]
    seen: dict[str, Path] = {}
    for scenario in scenarios:
        if scenario.id in seen:
            raise ScenarioError(
                f"duplicate scenario id {scenario.id!r} in {seen[scenario.id]} and {scenario.source}"
            )
        seen[scenario.id] = scenario.source
    return scenarios
