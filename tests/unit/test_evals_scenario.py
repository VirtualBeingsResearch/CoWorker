from __future__ import annotations

from pathlib import Path

import pytest
from evals.scenario import (
    SUPPORTED_LOCALES,
    ScenarioError,
    discover,
    load_scenario,
    localize,
)
from evals.workspace import REPO_ROOT, ModelTarget, child_env, prepare

BUNDLED = REPO_ROOT / "evals" / "scenarios"

MINIMAL = """
id: demo.case
kind: care
preregistration:
  hypothesis: something observable happens
script:
  - from: alice
    say:
      zh-CN: 你好
      en: hello
checks:
  - type: replied
    participant: alice
"""


def _write(tmp_path: Path, text: str, name: str = "case.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_bundled_scenarios_load_in_every_locale() -> None:
    scenarios = discover([BUNDLED])
    assert len(scenarios) >= 5
    for scenario in scenarios:
        assert scenario.kind == "care"
        assert scenario.preregistration.hypothesis
        assert set(scenario.locales) == set(SUPPORTED_LOCALES)
    life = [item for item in scenarios if item.id.startswith("life.")]
    assert {item.id for item in life} == {"life.one_week", "life.mature"}
    assert all(item.uses_timeline for item in life)


def test_minimal_scenario_defaults(tmp_path: Path) -> None:
    scenario = load_scenario(_write(tmp_path, MINIMAL))
    assert scenario.samples == 5
    assert scenario.guard.max_llm_calls == 200
    assert scenario.script[0].after == "settle"
    assert scenario.message_for(scenario.script[0], "en") == "hello"
    assert scenario.message_for(scenario.script[0], "zh-CN") == "你好"
    assert len(scenario.content_hash) == 16


def test_localize_resolves_nested_mappings() -> None:
    value = {"secrets": {"zh-CN": ["甲"], "en": ["A"]}, "min": 1}
    assert localize(value, "en") == {"secrets": ["A"], "min": 1}
    assert localize(value, "zh-CN") == {"secrets": ["甲"], "min": 1}


def test_missing_locale_text_is_rejected(tmp_path: Path) -> None:
    text = MINIMAL.replace("      en: hello\n", "")
    with pytest.raises(ScenarioError, match="missing 'en'"):
        load_scenario(_write(tmp_path, text))


@pytest.mark.parametrize("kind", ["ability", "experiment", "acquaintance"])
def test_reserved_kinds_are_not_runnable_yet(tmp_path: Path, kind: str) -> None:
    with pytest.raises(ScenarioError, match="reserved"):
        load_scenario(_write(tmp_path, MINIMAL.replace("kind: care", f"kind: {kind}")))


def test_unknown_check_type_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ScenarioError, match="unknown type"):
        load_scenario(_write(tmp_path, MINIMAL.replace("type: replied", "type: vibes")))


def test_preregistration_hypothesis_is_required(tmp_path: Path) -> None:
    text = MINIMAL.replace("  hypothesis: something observable happens\n", "  falsified_if: x\n")
    with pytest.raises(ScenarioError, match="hypothesis"):
        load_scenario(_write(tmp_path, text))


def test_invalid_guard_and_after_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ScenarioError, match="guard"):
        load_scenario(_write(tmp_path, MINIMAL + "guard: {max_tokens: 3}\n"))
    with pytest.raises(ScenarioError, match="after"):
        load_scenario(_write(tmp_path, MINIMAL.replace("  - from: alice\n", "  - from: alice\n    after: soon\n")))


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    _write(tmp_path, MINIMAL, "a.yaml")
    _write(tmp_path, MINIMAL, "b.yaml")
    with pytest.raises(ScenarioError, match="duplicate"):
        discover([tmp_path])


def test_config_maps_to_coworker_environment(tmp_path: Path, monkeypatch) -> None:
    text = MINIMAL + "config:\n  agent:\n    passive_mode: false\n  memory:\n    backend: file\n"
    scenario = load_scenario(_write(tmp_path, text))
    assert scenario.env_overrides() == {
        "AGENT__PASSIVE_MODE": "false",
        "MEMORY__BACKEND": "file",
    }
    monkeypatch.setenv("LLM__DEFAULT_MODEL", "leaked-from-host")
    monkeypatch.setenv("API__COMMUNICATION_TOKEN", "host-token")
    target = ModelTarget("zhipu", "glm-5.3-flash", api_keys={"LLM__ZHIPU_API_KEY": "k"})
    env = child_env(scenario, "en", target, 8123, "sample-token")
    assert env["LLM__DEFAULT_MODEL"] == "glm-5.3-flash"
    assert env["LLM__ZHIPU_API_KEY"] == "k"
    assert env["AGENT__PASSIVE_MODE"] == "false"
    assert env["I18N__LOCALE"] == "en"
    assert env["API__PORT"] == "8123"
    assert env["API__COMMUNICATION_TOKEN"] == "sample-token"


def test_prepare_writes_identity_and_files(tmp_path: Path) -> None:
    text = MINIMAL + "files:\n  shared/a.txt:\n    zh-CN: 甲\n    en: A\n"
    scenario = load_scenario(_write(tmp_path, text))
    workspace = tmp_path / "ws"
    prepare(workspace, scenario, "en", ModelTarget("p", "m"))
    assert (workspace / "shared" / "a.txt").read_text(encoding="utf-8") == "A"
    assert (workspace / "data" / "identity" / "name.txt").is_file()
    assert (workspace / "data" / "identity" / "current_location.txt").is_file()


def test_prepare_rejects_files_outside_workspace(tmp_path: Path) -> None:
    scenario = load_scenario(_write(tmp_path, MINIMAL + "files:\n  ../escape.txt: x\n"))
    with pytest.raises(ValueError, match="escapes"):
        prepare(tmp_path / "ws", scenario, "en", ModelTarget("p", "m"))


TIMELINE = """
id: demo.timeline
kind: care
preregistration:
  hypothesis: a message arrives later
clock:
  start: "2026-01-05T09:17:00+08:00"
  horizon: P1D
script:
  - at: "+0s"
    from: alice
    say:
      zh-CN: 你好
      en: hello
  - at: "+3h"
    action: restart
  - at: "+4h"
    from: alice
    say:
      zh-CN: 还在吗
      en: still there
checks:
  - type: replied
    participant: alice
"""


def test_timeline_script_parses_at_and_restart(tmp_path: Path) -> None:
    scenario = load_scenario(_write(tmp_path, TIMELINE))
    assert scenario.uses_timeline
    assert scenario.clock.start.endswith("+08:00")
    assert scenario.script[1].action == "restart"
    assert scenario.script[2].at_seconds == 4 * 3600
    workspace = tmp_path / "ws"
    prepare(workspace, scenario, "en", ModelTarget("p", "m"))
    script = (workspace / ".evals" / "script.json").read_text(encoding="utf-8")
    assert "still there" in script
    env = child_env(scenario, "en", ModelTarget("p", "m"), 1, "t", workspace=workspace)
    assert env["EVALS__TIMELINE"] == "1"
    assert env["EVALS__CLOCK_START"]


def test_timeline_rejects_mixed_at_and_after(tmp_path: Path) -> None:
    text = TIMELINE.replace("    action: restart\n", "    after: settle\n    from: bob\n    say: {zh-CN: 嗨, en: hi}\n")
    with pytest.raises(ScenarioError, match="mix"):
        load_scenario(_write(tmp_path, text))


def test_at_requires_clock_start(tmp_path: Path) -> None:
    text = """
id: demo.at
kind: care
preregistration:
  hypothesis: x
script:
  - at: "+0s"
    from: alice
    say: {zh-CN: 你好, en: hi}
checks:
  - type: replied
    participant: alice
"""
    with pytest.raises(ScenarioError, match="clock.start"):
        load_scenario(_write(tmp_path, text))


def test_clock_start_needs_timezone(tmp_path: Path) -> None:
    text = TIMELINE.replace("2026-01-05T09:17:00+08:00", "2026-01-05T09:17:00")
    with pytest.raises(ScenarioError, match="timezone"):
        load_scenario(_write(tmp_path, text))
