from __future__ import annotations

import importlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, cast

from coworker.core.config import Config
from coworker.memory.base import LongTermMemoryBackend
from coworker.memory.long_term import build_memory_llm_config

if TYPE_CHECKING:
    from coworker.memory.backends.file import FileBackend
    from coworker.memory.backends.mem0 import Mem0Backend

# 后端模块名 -> 模块内类名。仅用于“枚举有哪些后端”，不在模块顶层 import 任何后端
# 依赖：单个后端模块加载失败只会让该后端不可用，不影响其它后端与进程启动。
_BACKEND_ENTRIES: dict[str, str] = {
    "file": "FileBackend",
    "mem0": "Mem0Backend",
}


class BackendClass(Protocol):
    """后端实现类的类级契约（探测 + 可构造）。

    实例行为由 :class:`LongTermMemoryBackend` 描述；这里补充后端自曝的
    ``backend_id`` / ``required_modules()`` / ``available()`` 类级成员。
    """

    backend_id: ClassVar[str]

    @classmethod
    def required_modules(cls) -> tuple[str, ...]: ...

    @classmethod
    def available(cls) -> bool: ...

    def __call__(self, *args: Any, **kwargs: Any) -> LongTermMemoryBackend: ...


def _load_backends() -> dict[str, type[BackendClass]]:
    """容错加载所有后端类。

    某后端模块无法加载（例如未来某天在顶层 import 了未安装的第三方依赖）时，
    仅跳过该后端，不影响其它后端与 --check / 进程启动。
    """
    loaded: dict[str, type[BackendClass]] = {}
    for name, cls_name in _BACKEND_ENTRIES.items():
        try:
            module = importlib.import_module(f"coworker.memory.backends.{name}")
            cls = getattr(module, cls_name)
        except (ImportError, AttributeError):
            continue
        loaded[cls.backend_id] = cls
    return loaded


def available_backends() -> list[str]:
    """当前环境实际可用的后端名（仅 ``available()`` 为真的）。

    缺失某后端依赖时只把它从返回集合中剔除，绝不抛错。
    """
    return sorted(
        backend_id
        for backend_id, cls in _load_backends().items()
        if cls.available()
    )


def missing_backend_modules(backend_id: str) -> list[str]:
    """报错明细用：返回给定后端缺少的可导入顶层模块；均已满足时返回空列表。

    仅在 ``available(backend_id)`` 为 False 时才有非空意义的调用；对轻依赖后端
    （如 file）恒返回空。
    """
    for bid, cls in _load_backends().items():
        if bid == backend_id and not cls.available():
            import importlib.util

            return [
                module
                for module in cls.required_modules()
                if importlib.util.find_spec(module) is None
            ]
    return []


def build_long_term_backend(
    config: Config,
    *,
    active_provider: str = "",
    active_model: str = "",
    embedder_model: str | None = None,
) -> LongTermMemoryBackend:
    """Create the configured long-term memory backend.

    Only backends that perform LLM extraction (mem0 today) need the resolved
    memory LLM config, so it is built here instead of at the call site.

    If the configured backend is unavailable because its dependencies are not
    installed, a localized error with installation guidance is raised instead of
    failing later with a bare ImportError.
    """
    backends = _load_backends()
    cls = backends.get(config.memory.backend)
    if cls is None:
        raise ValueError(f"Unknown long-term memory backend: {config.memory.backend}")
    if not cls.available():
        from coworker.i18n import tr

        missing = ", ".join(missing_backend_modules(cls.backend_id)) or "mem0"
        raise RuntimeError(
            tr(
                "system.memory_backend_missing_deps",
                backend=cls.backend_id,
                missing=missing,
            )
        )

    backend_id = cls.backend_id
    if backend_id == "mem0":
        mem0_cls = cast("type[Mem0Backend]", cls)
        return mem0_cls(
            db_path=config.memory.db_path,
            llm=build_memory_llm_config(
                config,
                active_provider=active_provider,
                active_model=active_model,
            ),
            embedder_model=embedder_model or config.memory.mem0_embedder_model,
        )
    if backend_id == "file":
        file_cls = cast("type[FileBackend]", cls)
        return file_cls(directory=str(Path(config.memory.db_path) / "long_term"))
    raise ValueError(f"Unknown long-term memory backend: {config.memory.backend}")
