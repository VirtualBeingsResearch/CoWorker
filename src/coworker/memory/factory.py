from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, cast

from coworker.core.config import Config, MemoryBackend
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

# 记录首次成功初始化后端时实际使用的后端；此后启动保持该后端，不再随依赖变化回退。
_MEMORY_BACKEND_STATE_FILE = "memory_backend.json"


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


def memory_backend_state_path(db_path: str | Path) -> Path:
    """长期记忆后端状态文件；记录首次初始化成功时实际使用的后端。"""

    return Path(db_path) / _MEMORY_BACKEND_STATE_FILE


def load_memory_backend_state(db_path: str | Path) -> MemoryBackend | None:
    """读取已建立的长期记忆后端；没有记录、损坏或取值无效时返回 None。"""

    try:
        raw = json.loads(memory_backend_state_path(db_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    backend = raw.get("backend") if isinstance(raw, dict) else None
    if backend in ("mem0", "file"):
        return backend
    return None


def write_memory_backend_state(db_path: str | Path, backend_id: str) -> None:
    """记录已初始化的长期记忆后端，供后续启动保持后端不变。"""

    if backend_id not in _BACKEND_ENTRIES:
        return
    state_path = memory_backend_state_path(db_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(state_path.suffix + ".tmp")
    temporary.write_text(json.dumps({"backend": backend_id}), encoding="utf-8")
    temporary.replace(state_path)


def mem0_data_present(db_path: str | Path) -> bool:
    """升级兼容：没有状态文件、但目录里已有 mem0（Chroma）持久化数据。"""

    return (Path(db_path) / "chroma.sqlite3").exists()


def resolve_memory_backend(
    config: Config,
    *,
    backend_explicit: bool,
) -> tuple[MemoryBackend, bool]:
    """决定本次启动实际使用的长期记忆后端。

    返回 ``(backend_id, fell_back)``。只有在**尚未建立长期记忆后端**时才允许
    因 mem0 依赖缺失回退到 ``file``：没有状态记录、没有既有 mem0 数据，且用户
    没有显式指定 mem0。后端一旦初始化过，就按记录保持，不再随依赖增减切换；
    显式配置始终优先，缺依赖时仍由 :func:`build_long_term_backend` 给出安装引导。
    """
    configured = config.memory.backend
    if configured != "mem0":
        return configured, False
    if "mem0" in available_backends():
        return "mem0", False
    if backend_explicit:
        return "mem0", False
    recorded = load_memory_backend_state(config.memory.db_path)
    if recorded is not None:
        return recorded, False
    if mem0_data_present(config.memory.db_path):
        return "mem0", False
    return "file", True


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
            query_settings=config.memory,
        )
    if backend_id == "file":
        file_cls = cast("type[FileBackend]", cls)
        return file_cls(directory=str(Path(config.memory.db_path) / "long_term"))
    raise ValueError(f"Unknown long-term memory backend: {config.memory.backend}")
