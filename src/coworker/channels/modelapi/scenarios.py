"""On-disk storage for caller scenario material (system prompt + tools).

Caller material can be far larger than what belongs in a conversation
context, so each unique scenario is stored once as a document under its
content hash. The model receives a compact notice — what the material is,
where the document lives, and how to act on it — and reads the original on
demand with its file tools.
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from coworker.i18n import tr


class ScenarioStore:
    """Persist caller system prompts and tool schemas as readable documents."""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    def save(
        self,
        scenario_hash: str,
        system_text: str,
        tools: list[dict],
    ) -> Path | None:
        """Write the scenario document once per hash; return its path."""
        if not system_text and not tools:
            return None
        path = self._document_path(scenario_hash)
        if path.is_file():
            return path
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            path.write_text(
                self._render(scenario_hash, system_text, tools), encoding="utf-8"
            )
        except OSError as error:
            logger.warning(f"model API scenario document save failed: {error}")
            return None
        return path

    def _document_path(self, scenario_hash: str) -> Path:
        return self._directory / f"scenario_{scenario_hash}.md"

    def _render(
        self, scenario_hash: str, system_text: str, tools: list[dict]
    ) -> str:
        sections = [tr("channel.model_api.doc_title", hash=scenario_hash)]
        if system_text:
            sections.append(tr("channel.model_api.doc_system_section"))
            sections.append(system_text)
        if tools:
            sections.append(tr("channel.model_api.doc_tools_section"))
            sections.append(json.dumps(tools, ensure_ascii=False, indent=2))
        return "\n\n".join(sections)
