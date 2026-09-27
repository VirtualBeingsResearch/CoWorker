from __future__ import annotations

from evals.scenario import load_scenario
from evals.states import apply, pack
from evals.workspace import ModelTarget, prepare


def test_state_forks_do_not_share_writes(tmp_path) -> None:
    source = tmp_path / "origin"
    (source / "data" / "memory" / "long_term").mkdir(parents=True)
    (source / "data" / "memory" / "long_term" / "note.json").write_text("{}", encoding="utf-8")
    dest = pack(source, "unit-fork", root=tmp_path / "states")
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    apply(left, dest)
    apply(right, dest)
    (left / "data" / "memory" / "long_term" / "only-left.json").write_text("left", encoding="utf-8")
    assert not (right / "data" / "memory" / "long_term" / "only-left.json").exists()
    assert (right / "data" / "memory" / "long_term" / "note.json").is_file()


def test_prepare_applies_seeded_state(tmp_path) -> None:
    scenario_text = """
id: demo.seeded
kind: care
state: seeded
preregistration:
  hypothesis: seeded memories are present
script:
  - from: alice
    say: {zh-CN: 你好, en: hi}
checks:
  - type: replied
    participant: alice
"""
    path = tmp_path / "s.yaml"
    path.write_text(scenario_text, encoding="utf-8")
    scenario = load_scenario(path)
    workspace = tmp_path / "ws"
    prepare(workspace, scenario, "en", ModelTarget("virtual", "virtual-model"))
    assert (workspace / "data" / "memory" / "long_term" / "seeded_alice.json").is_file()
