from __future__ import annotations

import pytest
from evals.ability import discover_suites, load_suite, score_text
from evals.extract import extract_choice
from evals.judge import _parse_verdict, ensure_different_vendor
from evals.llm import vendor_of
from evals.workspace import REPO_ROOT, ModelTarget


def test_bundled_ability_suites_load() -> None:
    suites = discover_suites([REPO_ROOT / "evals" / "abilities"])
    ids = {suite.id for suite in suites}
    assert ids == {
        "ability.knowledge.zh",
        "ability.code.execute",
        "ability.task.dinner",
    }
    knowledge = next(suite for suite in suites if suite.domain == "knowledge")
    assert knowledge.scoring == "choice" and knowledge.book == "closed"
    dinner = next(suite for suite in suites if suite.domain == "task")
    assert dinner.max_user_turns >= 2 and dinner.scoring == "judge"


def test_choice_extraction_and_scoring() -> None:
    assert extract_choice("I think the answer is C.\nC").value == "C"
    assert extract_choice("no letters here").failed
    suite = load_suite(REPO_ROOT / "evals" / "abilities" / "knowledge_zh.yaml")
    item = suite.items[0]
    assert score_text(suite, item, "最后一行\nC")["passed"]
    assert not score_text(suite, item, "最后一行\nA")["passed"]
    assert score_text(suite, item, "没有选项")["extract_failed"]


def test_code_scoring_runs_main() -> None:
    suite = load_suite(REPO_ROOT / "evals" / "abilities" / "code_execute.yaml")
    item = next(entry for entry in suite.items if entry.id == "reverse-words")
    source = "def main():\n    print('friend there hello')\n"
    assert score_text(suite, item, f"```python\n{source}```")["passed"]


def test_judge_rejects_same_vendor() -> None:
    assert vendor_of("zhipu") != vendor_of("opencode-go")
    with pytest.raises(ValueError, match="same vendor"):
        ensure_different_vendor(ModelTarget("deepseek", "x"), ModelTarget("deepseek", "y"))
    ensure_different_vendor(ModelTarget("zhipu", "x"), ModelTarget("opencode-go", "deepseek-flash"))
    verdict = _parse_verdict("PASS\n她记下了时间")
    assert verdict.passed
    assert not _parse_verdict("FAIL\n没有写文件").passed
