from __future__ import annotations

import importlib
import importlib.util

import pytest

from coworker.core.config import Config
from coworker.memory.backends.file import FileBackend
from coworker.memory.backends.mem0 import Mem0Backend
from coworker.memory.factory import (
    _load_backends,
    available_backends,
    build_long_term_backend,
    missing_backend_modules,
)


def test_file_backend_is_always_available() -> None:
    assert FileBackend.available() is True
    assert FileBackend.required_modules() == ()


def test_mem0_backend_missing_dependency_is_not_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None if name == "mem0" else True)
    assert Mem0Backend.available() is False
    # 仅列出缺失的顶层模块 —— mem0 缺失即视为不可用
    assert Mem0Backend.required_modules() == ("mem0",)


def test_mem0_backend_available_when_dependency_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: True)
    assert Mem0Backend.available() is True


def test_available_backends_excludes_mem0_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None if name == "mem0" else True)
    assert available_backends() == ["file"]


def test_available_backends_includes_mem0_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: True)
    assert available_backends() == ["file", "mem0"]


def test_missing_backend_modules_report_details(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None if name == "mem0" else True)
    assert missing_backend_modules("mem0") == ["mem0"]
    # file 后端无额外依赖，恒不报告缺失
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert missing_backend_modules("file") == []


def test_tolerant_loader_skips_broken_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """单个后端模块加载失败只跳过该后端，绝不影响其它后端与进程启动。"""
    real_import_module = importlib.import_module

    def fake_import(name: str):
        if name == "coworker.memory.backends.mem0":
            raise ImportError("simulated broken mem0 backend module")
        return real_import_module(name)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    loaded = _load_backends()
    assert "mem0" not in loaded
    assert "file" in loaded
    # available_backends() 仍正常返回，不抛错
    assert "file" in available_backends()


def test_build_long_term_backend_raises_localized_error_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config.model_validate({"memory": {"backend": "mem0"}})
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None if name == "mem0" else True)
    with pytest.raises(RuntimeError) as excinfo:
        build_long_term_backend(config)
    assert "uv sync --extra mem0" in str(excinfo.value)


def test_build_mem0_backend_reads_relevance_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.model_validate(
        {"memory": {"backend": "mem0", "auto_recall_relevance_threshold": 0.72}}
    )
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: True)

    backend = build_long_term_backend(config)

    assert isinstance(backend, Mem0Backend)
    assert backend.relevance_threshold == 0.72

    config.memory.auto_recall_relevance_threshold = 0.81
    assert backend.relevance_threshold == 0.81
