from __future__ import annotations

from pathlib import Path

import yaml
from loguru import logger


def _as_str_list(value: object) -> list[str]:
    """Normalize a frontmatter field into a list of non-empty strings.

    Accepts a YAML list (``[a, b]``) or a comma-separated string (``"a, b"``).
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


class Palace:
    def __init__(
        self,
        name: str,
        when_to_attach: str,
        body: str,
        critical_skills: list[str] | None = None,
        related_skills: list[str] | None = None,
        memory_tags: list[str] | None = None,
    ) -> None:
        self.name = name
        self.when_to_attach = when_to_attach
        self.body = body
        self.critical_skills = critical_skills or []
        self.related_skills = related_skills or []
        self.memory_tags = memory_tags or []


class PalaceLoader:
    """Loads `.coworker/palaces/<name>/PALACE.md` domain bundles.

    Mirrors `SkillLoader`, but parses `when_to_attach / critical_skills /
    related_skills / memory_tags` as first-class fields. A palace is a thin
    composition layer (a "card" + pointers), not a store: the card body stays
    small and resident-friendly, while procedures live in skills and facts in
    long-term memory.
    """

    def __init__(self, palaces_dir: str) -> None:
        self._dir = Path(palaces_dir)
        self._palaces: dict[str, Palace] = {}
        self._active_load_warnings: dict[str, str] = {}
        self._pending_load_warnings: list[str] = []

    def load_all(self) -> None:
        self._palaces.clear()
        warnings: dict[str, str] = {}
        if not self._dir.exists():
            self._refresh_load_warnings(warnings)
            return
        for palace_dir in sorted(self._dir.iterdir()):
            if not palace_dir.is_dir():
                continue
            palace_file = palace_dir / "PALACE.md"
            if palace_file.exists():
                palace, warning = self._parse(palace_file)
                if warning:
                    warnings[str(palace_file)] = warning
                if palace:
                    existing = self._palaces.get(palace.name)
                    if existing is not None:
                        warnings[f"duplicate:{palace.name}:{palace_file}"] = (
                            f"Palace '{palace.name}' 重名，文件 {palace_file} 被跳过；"
                            f"已保留先加载的定义。"
                        )
                        logger.warning(warnings[f"duplicate:{palace.name}:{palace_file}"])
                        continue
                    self._palaces[palace.name] = palace
        self._refresh_load_warnings(warnings)
        logger.debug(f"Loaded {len(self._palaces)} palaces: {list(self._palaces.keys())}")

    def _parse(self, path: Path) -> tuple[Palace | None, str | None]:
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:
            warning = f"Palace 文件 {path} 读取失败：{type(e).__name__}: {e}"
            logger.warning(warning)
            return None, warning
        if not text.startswith("---"):
            warning = f"Palace 文件 {path} 缺少 frontmatter，已跳过。"
            logger.warning(warning)
            return None, warning
        parts = text.split("---", 2)
        if len(parts) < 3:
            warning = f"Palace 文件 {path} 的 frontmatter 结构不完整，已跳过。"
            logger.warning(warning)
            return None, warning
        try:
            fm: dict = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError as e:
            warning = f"Palace 文件 {path} 的 YAML frontmatter 解析失败：{e}"
            logger.warning(warning)
            return None, warning
        name = fm.get("name", "")
        if not name:
            warning = f"Palace 文件 {path} 缺少 name 字段，已跳过。"
            logger.warning(warning)
            return None, warning
        return Palace(
            name=str(name),
            when_to_attach=str(fm.get("when_to_attach", "")),
            body=parts[2].strip(),
            critical_skills=_as_str_list(fm.get("critical_skills")),
            related_skills=_as_str_list(fm.get("related_skills")),
            memory_tags=_as_str_list(fm.get("memory_tags")),
        ), None

    def _refresh_load_warnings(self, warnings: dict[str, str]) -> None:
        self._pending_load_warnings = [
            message
            for key, message in warnings.items()
            if self._active_load_warnings.get(key) != message
        ]
        self._active_load_warnings = warnings

    def consume_load_warnings(self) -> list[str]:
        warnings = list(self._pending_load_warnings)
        self._pending_load_warnings.clear()
        return warnings

    def get(self, name: str) -> Palace | None:
        return self._palaces.get(name)

    def list_all(self) -> list[Palace]:
        return list(self._palaces.values())

    def list_names(self) -> list[str]:
        return list(self._palaces.keys())

    def format_for_prompt(self) -> str:
        """Render the thin resident registry: one line per palace.

        Only `name` + `when_to_attach` so the system-prompt prefix stays stable
        and cache-friendly — the full card is loaded into the bubble on attach.
        """
        self.load_all()
        if not self._palaces:
            return ""
        return "\n".join(
            f"- {p.name}: {p.when_to_attach}" for p in self._palaces.values()
        )
