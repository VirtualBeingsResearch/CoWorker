from __future__ import annotations

import importlib.util

import pytest

from coworker.application import _validate_backend_available
from coworker.core.config import Config


def _config(backend: str) -> Config:
    return Config.model_validate({"memory": {"backend": backend}})


def test_passes_when_file_backend_configured_and_available() -> None:
    _validate_backend_available(_config("file"))


def test_passes_when_mem0_backend_configured_and_available(monkeypatch: pytest.MonkeyPatch) -> None:
    # 模拟 mem0 依赖齐全：mem0 在可用集合中
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: True)
    _validate_backend_available(_config("mem0"))


def test_raises_localized_guidance_when_mem0_configured_but_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None if name == "mem0" else True)
    with pytest.raises(RuntimeError) as excinfo:
        _validate_backend_available(_config("mem0"))
    message = str(excinfo.value)
    assert "mem0" in message
    assert "uv sync --extra mem0" in message
