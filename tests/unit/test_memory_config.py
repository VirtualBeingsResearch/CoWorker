from __future__ import annotations

import pytest

from coworker.core.config import MemoryConfig


def test_default_backend_is_mem0_without_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMORY_DEFAULT_BACKEND", raising=False)
    monkeypatch.delenv("MEMORY__BACKEND", raising=False)
    config = MemoryConfig(_env_file=None)
    assert config.backend == "mem0"


def test_default_backend_follows_memory_default_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_DEFAULT_BACKEND", "file")
    monkeypatch.delenv("MEMORY__BACKEND", raising=False)
    config = MemoryConfig(_env_file=None)
    assert config.backend == "file"


def test_explicit_memory_backend_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_DEFAULT_BACKEND", "file")
    monkeypatch.setenv("MEMORY__BACKEND", "mem0")
    config = MemoryConfig(_env_file=None)
    assert config.backend == "mem0"


def test_invalid_default_backend_falls_back_to_mem0(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_DEFAULT_BACKEND", "bogus")
    monkeypatch.delenv("MEMORY__BACKEND", raising=False)
    config = MemoryConfig(_env_file=None)
    assert config.backend == "mem0"


def test_summary_budget_retry_and_tolerance_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMORY__SUMMARY_BUDGET_RETRIES", raising=False)
    monkeypatch.delenv("MEMORY__SUMMARY_BUDGET_TOLERANCE", raising=False)
    config = MemoryConfig(_env_file=None)
    assert config.summary_budget_retries == 3
    assert config.summary_budget_tolerance == pytest.approx(0.10)


def test_summary_budget_retry_and_tolerance_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY__SUMMARY_BUDGET_RETRIES", "5")
    monkeypatch.setenv("MEMORY__SUMMARY_BUDGET_TOLERANCE", "0.25")
    config = MemoryConfig(_env_file=None)
    assert config.summary_budget_retries == 5
    assert config.summary_budget_tolerance == pytest.approx(0.25)
