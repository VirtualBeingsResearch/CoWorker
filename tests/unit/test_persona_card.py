"""Tests for the persona card file store."""

from __future__ import annotations

from coworker.persona import PersonaCard


def test_save_load_round_trip(tmp_path) -> None:
    cards = PersonaCard(tmp_path)
    cards.save("p_abc", "# 张三\n- 称呼：张三")
    assert cards.load("p_abc") == "# 张三\n- 称呼：张三"


def test_load_missing_returns_empty(tmp_path) -> None:
    cards = PersonaCard(tmp_path)
    assert cards.load("p_nope") == ""


def test_delete_removes_file(tmp_path) -> None:
    cards = PersonaCard(tmp_path)
    cards.save("p_abc", "content")
    cards.delete("p_abc")
    assert cards.load("p_abc") == ""
    cards.delete("p_abc")  # idempotent
