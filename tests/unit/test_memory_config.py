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
