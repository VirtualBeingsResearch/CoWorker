"""Scenario definitions: loading, validation, and locale resolution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SUPPORTED_LOCALES = ("zh-CN", "en")
SUPPORTED_KINDS = ("care",)
RESERVED_KINDS = ("ability", "experiment", "acquaintance")
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
class Step:
    participant: str
    say: Any
    after: str | float = SETTLE


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


def _parse_step(raw: Any, index: int, source: Path) -> Step:
    if not isinstance(raw, dict):
        raise ScenarioError(f"{source}: script[{index}] must be a mapping")
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
    return Step(participant=participant, say=_require(raw, "say", source), after=after)


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

    scenario = Scenario(
        id=str(_require(data, "id", source)),
        version=int(data.get("version", 1)),
        kind=kind,
        preregistration=preregistration,
        locales=locales,
        samples=int(data.get("samples", 5)),
        guard=guard,
        config={section: dict(values or {}) for section, values in config.items()},
        files=dict(data.get("files") or {}),
        script=tuple(_parse_step(item, i, source) for i, item in enumerate(script_raw)),
        checks=tuple(_parse_check(item, i, source) for i, item in enumerate(checks_raw)),
        source=source,
        content_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:16],
    )
    for locale in scenario.locales:
        for step in scenario.script:
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
