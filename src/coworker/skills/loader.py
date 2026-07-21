from __future__ import annotations

from pathlib import Path

import yaml
from loguru import logger

from coworker.i18n import tr
from coworker.i18n.resources import load_markdown_companion


class Skill:
    def __init__(
        self,
        name: str,
        description: str,
        body: str,
        version: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.body = body
        self.version = version
        self.metadata = metadata or {}


class SkillLoader:
    def __init__(self, skills_dir: str) -> None:
        self._dir = Path(skills_dir)
        self._skills: dict[str, Skill] = {}
        self._active_skill_load_warnings: dict[str, str] = {}
        self._pending_skill_load_warnings: list[str] = []

    def load_all(self) -> None:
        self._skills.clear()
        warnings: dict[str, str] = {}
        if not self._dir.exists():
            self._refresh_skill_load_warnings(warnings)
            return
        for skill_dir in sorted(self._dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if skill_file.exists():
                skill, warning = self._parse(skill_file)
                if warning:
                    warnings[str(skill_file)] = warning
                if skill:
                    existing = self._skills.get(skill.name)
                    if existing is not None:
                        warnings[f"duplicate:{skill.name}:{skill_file}"] = tr(
                            "assets.duplicate",
                            kind="Skill",
                            name=skill.name,
                            path=skill_file,
                        )
                        logger.warning(warnings[f"duplicate:{skill.name}:{skill_file}"])
                        continue
                    self._skills[skill.name] = skill
        self._refresh_skill_load_warnings(warnings)
        logger.debug(f"Loaded {len(self._skills)} skills: {list(self._skills.keys())}")

    def _parse(self, path: Path) -> tuple[Skill | None, str | None]:
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:
            warning = tr(
                "assets.read_failed",
                kind="Skill",
                path=path,
                error_type=type(e).__name__,
                error=e,
            )
            logger.warning(warning)
            return None, warning
        if not text.startswith("---"):
            warning = tr("assets.asset_frontmatter_missing", kind="Skill", path=path)
            logger.warning(warning)
            return None, warning
        parts = text.split("---", 2)
        if len(parts) < 3:
            warning = tr("assets.asset_frontmatter_incomplete", kind="Skill", path=path)
            logger.warning(warning)
            return None, warning
        try:
            fm: dict = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError as e:
            warning = tr("assets.yaml_failed", kind="Skill", path=path, error=e)
            logger.warning(warning)
            return None, warning
        name = fm.get("name", "")
        if not name:
            warning = tr("assets.name_missing", kind="Skill", path=path)
            logger.warning(warning)
            return None, warning
        base_body = parts[2].strip()
        localized = load_markdown_companion(
            path,
            base_fields=fm,
            base_body=base_body,
            localizable_fields=("description",),
        )
        if localized.warning:
            logger.warning(localized.warning)
        return Skill(
            name=str(name),
            description=localized.fields["description"],
            body=localized.body,
            version=fm.get("version") or (fm.get("metadata") or {}).get("version"),
            metadata=fm.get("metadata") or {},
        ), localized.warning

    def _refresh_skill_load_warnings(self, warnings: dict[str, str]) -> None:
        self._pending_skill_load_warnings = [
            message
            for key, message in warnings.items()
            if self._active_skill_load_warnings.get(key) != message
        ]
        self._active_skill_load_warnings = warnings

    def consume_skill_load_warnings(self) -> list[str]:
        warnings = list(self._pending_skill_load_warnings)
        self._pending_skill_load_warnings.clear()
        return warnings

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def list_names(self) -> list[str]:
        return list(self._skills.keys())

    def get_relevant_skills(self, context: str) -> list[Skill]:
        self.load_all()
        return list(self._skills.values())

    def format_for_prompt(self, context: str = "") -> str:
        skills = self.get_relevant_skills(context)
        if not skills:
            return ""
        parts = []
        for skill in skills:
            parts.append(f"- {skill.name}: {skill.description}")
        return "\n".join(parts)
