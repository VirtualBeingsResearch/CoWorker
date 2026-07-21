from __future__ import annotations

import os
import platform
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from coworker.core.constants import TICK_TAG
from coworker.i18n import get_locale, tr
from coworker.i18n.resources import read_localized_text


def _tz_info() -> str:
    offset_secs = -time.timezone
    if time.daylight and time.localtime().tm_isdst:
        offset_secs = -time.altzone
    hours, remainder = divmod(abs(offset_secs), 3600)
    minutes = remainder // 60
    sign = "+" if offset_secs >= 0 else "-"
    offset_str = f"UTC{sign}{hours}" if minutes == 0 else f"UTC{sign}{hours}:{minutes:02d}"
    tz_name = time.tzname[1 if (time.daylight and time.localtime().tm_isdst) else 0]
    return f"{tz_name} ({offset_str})"


def _build_env_section(git_commit: str | None = None) -> str:
    lines = [
        "[ENVIRONMENT]",
        tr("prompt.environment.os", value=f"{platform.system()} {platform.release()}"),
        tr("prompt.environment.architecture", value=platform.machine()),
        tr("prompt.environment.python_version", value=sys.version.split()[0]),
        tr("prompt.environment.python_executable", value=sys.executable),
        tr("prompt.environment.working_directory", value=os.getcwd()),
        tr("prompt.environment.timezone", value=_tz_info()),
    ]
    if git_commit:
        lines.append(tr("prompt.environment.git_commit", value=git_commit))
    return "\n".join(lines)


if TYPE_CHECKING:
    from coworker.identity.identity import Identity
    from coworker.palaces.loader import PalaceLoader
    from coworker.skills.loader import SkillLoader
    from coworker.tools.registry import ToolRegistry

class SystemPromptBuilder:
    def __init__(
        self,
        identity: Identity,
        tool_registry: ToolRegistry,
        skill_loader: SkillLoader,
        palace_loader: PalaceLoader | None = None,
        thinking_path: str | Path = "data/thinking.md",
        git_commit: str | None = None,
    ) -> None:
        self._identity = identity
        self._tools = tool_registry
        self._skills = skill_loader
        self._palaces = palace_loader
        self._thinking_path = Path(thinking_path)
        self._git_commit = git_commit
        # 系统提示词整体缓存，只在首次 build 或显式 refresh() 后重建。
        # 模型刚写入 skill / thinking / identity 时，变更内容仍在短期上下文里；
        # 等记忆压缩导致上下文缓存失效，再统一刷新系统提示词，避免每轮扫盘和打掉前缀缓存。
        self._cached_prompts: dict[str, str] = {}

    def build(self) -> str:
        locale = get_locale()
        cached = self._cached_prompts.get(locale.value)
        if cached is not None:
            return cached

        sections: list[str] = []

        sections.append(f"[IDENTITY]\n{self._identity.to_system_prompt_section()}")

        sections.append(_build_env_section(self._git_commit))
        instinct_parts = [
            tr(
                "prompt.instincts_intro",
                count=tr("prompt.count_six" if not self._identity.name else "prompt.count_five"),
            ),
            tr("prompt.instincts", tick_tag=TICK_TAG),
        ]
        if not self._identity.name:
            instinct_parts.append(tr("prompt.newborn_instinct"))
        sections.append(f"[INSTINCTS]\n{'\n'.join(instinct_parts)}")
        sections.append(f"[GUIDELINES]\n{tr('prompt.guidelines')}")
        sections.append(tr("prompt.language_policy", locale=locale.value))

        thinking_text = self._read_thinking()
        if thinking_text:
            sections.append(f"[THINKING]\n{thinking_text}")

        skills_text = self._skills.format_for_prompt()
        if skills_text:
            sections.append(f"[SKILLS]\n{tr('prompt.skills_intro')}\n\n{skills_text}")

        if self._palaces is not None:
            palaces_text = self._palaces.format_for_prompt()
            if palaces_text:
                sections.append(f"[PALACES]\n{tr('prompt.palaces_intro')}\n\n{palaces_text}")

        prompt = "\n\n".join(sections) + "\n"
        self._cached_prompts[locale.value] = prompt
        return prompt

    def _read_thinking(self) -> str:
        return read_localized_text(self._thinking_path)

    def refresh(self) -> None:
        """Invalidate and reload prompt inputs after context cache invalidation."""
        self._identity.load()
        self._cached_prompts.clear()

    def consume_skill_load_warnings(self) -> list[str]:
        return self._skills.consume_skill_load_warnings()

    def skill_body(self, name: str) -> str:
        self._skills.load_all()
        skill = self._skills.get(name)
        return skill.body if skill is not None else ""
