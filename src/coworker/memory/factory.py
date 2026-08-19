from __future__ import annotations

from pathlib import Path

from coworker.core.config import Config
from coworker.memory.backends.file import FileBackend
from coworker.memory.backends.mem0 import Mem0Backend
from coworker.memory.base import LongTermMemoryBackend
from coworker.memory.long_term import build_memory_llm_config


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
    """
    backend = config.memory.backend
    if backend == "mem0":
        return Mem0Backend(
            db_path=config.memory.db_path,
            llm=build_memory_llm_config(
                config,
                active_provider=active_provider,
                active_model=active_model,
            ),
            embedder_model=embedder_model or config.memory.mem0_embedder_model,
        )
    if backend == "file":
        return FileBackend(directory=str(Path(config.memory.db_path) / "long_term"))
    raise ValueError(f"Unknown long-term memory backend: {backend}")
