from __future__ import annotations

import importlib.util

import pytest

from coworker.application import _validate_backend_available
from coworker.core.config import Config


def _config(backend: str) -> Config:
    return Config.model_validate({"memory": {"backend": backend}})


def test_passes_when_file_backend_configured_and_available() -> None:
    _validate_backend_available(_config("file"))


def test_passes_with_default_config_when_mem0_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """未显式配置后端时，缺 mem0 依赖会回退到 file，启动校验不拦截。"""
    monkeypatch.delenv("MEMORY__BACKEND", raising=False)
    monkeypatch.delenv("MEMORY_DEFAULT_BACKEND", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None if name == "mem0" else True)
    config = Config.model_validate({})
    assert config.memory.backend == "file"
    _validate_backend_available(config)


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
