from __future__ import annotations

from pathlib import Path

from coworker.core.config import Config
from coworker.memory.backends.file import FileBackend
from coworker.memory.backends.mem0 import Mem0Backend
from coworker.memory.base import LongTermMemoryBackend, MemoryBackendConfig


def build_long_term_backend(
    config: Config,
    *,
    llm: MemoryBackendConfig | None = None,
    embedder_model: str | None = None,
) -> LongTermMemoryBackend:
    """Create the configured long-term memory backend."""
    backend = config.memory.backend
    if backend == "mem0":
        return Mem0Backend(
            db_path=config.memory.db_path,
            llm=llm,
            embedder_model=embedder_model or config.memory.mem0_embedder_model,
        )
    if backend == "file":
        return FileBackend(directory=str(Path(config.memory.db_path) / "long_term"))
    raise ValueError(f"Unknown long-term memory backend: {backend}")
