from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from loguru import logger

from coworker.palaces.loader import _as_str_list

if TYPE_CHECKING:
    from coworker.agent.bubble import Bubble


_VALID_TRIGGERS = {"periodic", "garden", "cold_floor", "manual"}
_VALID_CONTEXT_BUILDERS = {"short_term", "garden"}


class SubconsciousMode:
    """A loaded subconscious mode definition.

    Loaded from `.coworker/subconscious/<name>/MODE.md`.
    Body is the identity prompt template; placeholders {bubble_id}, {goal},
    {max_cycles} are substituted at spawn time via render_identity().
    """

    def __init__(
        self,
        name: str,
        body: str,
        enabled: bool = True,
        trigger: str = "periodic",
        context_builder: str = "short_term",
        every_n_cycles: int = 0,
        every_seconds: int = 0,
        every_n_tool_calls: int = 0,
        cold_floor_seconds: int = 0,
        max_cycles: int = 0,
        goal: str = "",
        extra_intercepts: list[str] | None = None,
        grants_task_store: bool = False,
        inject_skill_anomalies: bool = False,
        inject_telemetry: bool = False,
        fresh_start: bool = False,
        use_threshold: int = 0,
        min_interval_seconds: int = 0,
        purpose: str = "",
        retire_after: str = "",
        protected: bool = False,
    ) -> None:
        self.name = name
        self.body = body
        self.enabled = enabled
        self.trigger = trigger
        self.context_builder = context_builder
        self.every_n_cycles = every_n_cycles
        self.every_seconds = every_seconds
        self.every_n_tool_calls = every_n_tool_calls
        self.cold_floor_seconds = cold_floor_seconds
        self.max_cycles = max_cycles
        self.goal = goal
        self.extra_intercepts: list[str] = extra_intercepts or []
        self.grants_task_store = grants_task_store
        self.inject_skill_anomalies = inject_skill_anomalies
        self.inject_telemetry = inject_telemetry
        self.fresh_start = fresh_start
        self.use_threshold = use_threshold
        self.min_interval_seconds = min_interval_seconds
        # Lifecycle fields.
        # purpose: WHY this mode exists — the problem it solves. Meta uses this to judge
        #          whether the mode's reason for being still applies to the current context.
        self.purpose = purpose
        # retire_after: free-text condition under which this mode should be considered for
        #               retirement (e.g. "切换到新监督机制后" or "2026-09-01 后"). Empty = no
        #               explicit trigger. Meta checks this and creates a [潜意识] retirement task
        #               when the condition appears to be met.
        self.retire_after = retire_after
        # protected: meta must NEVER suggest disabling, archiving, or deleting this mode,
        #            regardless of telemetry. Use for core safety/integrity modes whose
        #            absence would silently degrade the system (e.g. audit, introspect).
        self.protected = protected

    def render_identity(self, bubble: Bubble) -> str:
        """Substitute the 3 bubble-level placeholders.

        Uses str.replace (not str.format) to avoid KeyErrors from curly braces
        in example JSON or template text inside the body.
        """
        return (
            self.body
            .replace("{bubble_id}", bubble.id)
            .replace("{goal}", bubble.goal)
            .replace("{max_cycles}", str(bubble.max_cycles))
        )


class SubconsciousModeLoader:
    """Loads `.coworker/subconscious/<name>/MODE.md` files.

    Mirrors PalaceLoader: frontmatter (YAML) contains scheduling parameters and
    feature flags; the body is the identity prompt template for that mode.
    """

    def __init__(self, modes_dir: str) -> None:
        self._dir = Path(modes_dir)
        self._modes: dict[str, SubconsciousMode] = {}
        self._active_load_warnings: dict[str, str] = {}
        self._pending_load_warnings: list[str] = []

    def load_all(self) -> None:
        if not self._dir.exists():
            # No-op if directory is absent: preserves in-memory modes set by callers
            # (e.g. tests), avoids wiping a live system if the dir is temporarily missing.
            return
        self._modes.clear()
        warnings: dict[str, str] = {}
        for mode_dir in sorted(self._dir.iterdir()):
            if not mode_dir.is_dir():
                continue
            if mode_dir.name == "archived":
                continue  # Archived modes are intentionally retired; loader ignores them.
            mode_file = mode_dir / "MODE.md"
            if not mode_file.exists():
                continue
            mode, warning = self._parse(mode_file)
            if warning:
                warnings[str(mode_file)] = warning
            if mode:
                existing = self._modes.get(mode.name)
                if existing is not None:
                    msg = (
                        f"SubconsciousMode '{mode.name}' 重名，文件 {mode_file} 被跳过；"
                        f"已保留先加载的定义。"
                    )
                    warnings[f"duplicate:{mode.name}:{mode_file}"] = msg
                    logger.warning(msg)
                    continue
                self._modes[mode.name] = mode
        self._refresh_load_warnings(warnings)
        logger.debug(f"Loaded {len(self._modes)} subconscious modes: {list(self._modes.keys())}")

    def _parse(self, path: Path) -> tuple[SubconsciousMode | None, str | None]:
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:
            warning = f"MODE 文件 {path} 读取失败：{type(e).__name__}: {e}"
            logger.warning(warning)
            return None, warning
        if not text.startswith("---"):
            warning = f"MODE 文件 {path} 缺少 frontmatter，已跳过。"
            logger.warning(warning)
            return None, warning
        parts = text.split("---", 2)
        if len(parts) < 3:
            warning = f"MODE 文件 {path} 的 frontmatter 结构不完整，已跳过。"
            logger.warning(warning)
            return None, warning
        try:
            fm: dict = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError as e:
            warning = f"MODE 文件 {path} 的 YAML frontmatter 解析失败：{e}"
            logger.warning(warning)
            return None, warning
        name = str(fm.get("name", "")).strip()
        if not name:
            warning = f"MODE 文件 {path} 缺少 name 字段，已跳过。"
            logger.warning(warning)
            return None, warning
        body = parts[2].strip()

        trigger = str(fm.get("trigger", "periodic"))
        if trigger not in _VALID_TRIGGERS:
            logger.warning(f"MODE 文件 {path} 的 trigger='{trigger}' 无效，回退为 'periodic'。")
            trigger = "periodic"

        context_builder = str(fm.get("context_builder", "short_term"))
        if context_builder not in _VALID_CONTEXT_BUILDERS:
            logger.warning(f"MODE 文件 {path} 的 context_builder='{context_builder}' 无效，回退为 'short_term'。")
            context_builder = "short_term"

        return SubconsciousMode(
            name=name,
            body=body,
            enabled=bool(fm.get("enabled", True)),
            trigger=trigger,
            context_builder=context_builder,
            every_n_cycles=int(fm.get("every_n_cycles", 0) or 0),
            every_seconds=int(fm.get("every_seconds", 0) or 0),
            every_n_tool_calls=int(fm.get("every_n_tool_calls", 0) or 0),
            cold_floor_seconds=int(fm.get("cold_floor_seconds", 0) or 0),
            max_cycles=int(fm.get("max_cycles", 0) or 0),
            goal=str(fm.get("goal", "") or ""),
            extra_intercepts=_as_str_list(fm.get("extra_intercepts")),
            grants_task_store=bool(fm.get("grants_task_store", False)),
            inject_skill_anomalies=bool(fm.get("inject_skill_anomalies", False)),
            inject_telemetry=bool(fm.get("inject_telemetry", False)),
            fresh_start=bool(fm.get("fresh_start", False)),
            use_threshold=int(fm.get("use_threshold", 0) or 0),
            min_interval_seconds=int(fm.get("min_interval_seconds", 0) or 0),
            purpose=str(fm.get("purpose", "") or ""),
            retire_after=str(fm.get("retire_after", "") or ""),
            protected=bool(fm.get("protected", False)),
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

    def get(self, name: str) -> SubconsciousMode | None:
        return self._modes.get(name)

    def list_all(self) -> list[SubconsciousMode]:
        return [m for m in self._modes.values() if m.enabled]

    def list_names(self) -> list[str]:
        return list(self._modes.keys())
