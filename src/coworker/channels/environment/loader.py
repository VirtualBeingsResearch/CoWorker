"""Discover and parse environment source definitions.

Mirrors :class:`~coworker.skills.loader.SkillLoader`: each source lives in a
subdirectory of ``.coworker/environment/`` and is described by a
``SOURCE.md`` file with YAML frontmatter.  The Markdown body is kept as
human-readable documentation (like ``SKILL.md`` bodies).

Frontmatter fields map 1:1 onto :class:`EnvironmentSourceDef`.  Unknown keys
are ignored so authors can stash notes in frontmatter without breaking the
loader.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from loguru import logger

from coworker.i18n import tr
from coworker.i18n.resources import load_markdown_companion

from .types import EnvironmentSourceDef

_VALID_MODES = {"inline", "subprocess"}
_VALID_TRIGGERS = {"periodic", "cold_floor", "manual"}


class EnvironmentLoader:
    """Scans ``sources_dir`` for ``SOURCE.md`` files and parses them.

    Reload is cheap and idempotent: callers invoke :meth:`load_all` to refresh
    the in-memory dictionary (mirroring how the skill loader is called on each
    prompt build).  Warnings are accumulated and surfaced once via
    :meth:`consume_load_warnings`.
    """

    def __init__(self, sources_dir: str) -> None:
        self._dir = Path(sources_dir)
        self._sources: dict[str, EnvironmentSourceDef] = {}
        self._active_warnings: dict[str, str] = {}
        self._pending_warnings: list[str] = []

    @property
    def sources_dir(self) -> Path:
        return self._dir

    def load_all(self) -> None:
        self._sources.clear()
        warnings: dict[str, str] = {}
        if not self._dir.exists():
            self._refresh_warnings(warnings)
            return
        for source_dir in sorted(self._dir.iterdir()):
            if not source_dir.is_dir():
                continue
            source_file = source_dir / "SOURCE.md"
            if not source_file.exists():
                continue
            definition, warning = self._parse(source_file)
            if warning:
                warnings[str(source_file)] = warning
            if definition is not None:
                existing = self._sources.get(definition.name)
                if existing is not None:
                    key = f"duplicate:{definition.name}:{source_file}"
                    msg = tr(
                        "assets.duplicate",
                        kind="Environment source",
                        name=definition.name,
                        path=source_file,
                    )
                    warnings[key] = msg
                    logger.warning(msg)
                    continue
                self._sources[definition.name] = definition
        self._refresh_warnings(warnings)
        logger.debug(
            f"Loaded {len(self._sources)} environment sources: "
            f"{list(self._sources.keys())}"
        )

    def _parse(self, path: Path) -> tuple[EnvironmentSourceDef | None, str | None]:
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:
            warning = tr(
                "assets.read_failed",
                kind="Environment source",
                path=path,
                error_type=type(exc).__name__,
                error=exc,
            )
            logger.warning(warning)
            return None, warning

        if not text.startswith("---"):
            warning = tr(
                "assets.asset_frontmatter_missing",
                kind="Environment source",
                path=path,
            )
            logger.warning(warning)
            return None, warning

        parts = text.split("---", 2)
        if len(parts) < 3:
            warning = tr(
                "assets.asset_frontmatter_incomplete",
                kind="Environment source",
                path=path,
            )
            logger.warning(warning)
            return None, warning

        try:
            frontmatter: dict = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError as exc:
            warning = tr(
                "assets.yaml_failed",
                kind="Environment source",
                path=path,
                error=exc,
            )
            logger.warning(warning)
            return None, warning

        if not isinstance(frontmatter, dict):
            warning = tr(
                "assets.frontmatter_not_mapping",
            )
            logger.warning(f"Environment source {path}: {warning}")
            return None, warning

        name = str(frontmatter.get("name") or "").strip()
        if not name:
            warning = tr(
                "assets.name_missing",
                kind="Environment source",
                path=path,
            )
            logger.warning(warning)
            return None, warning

        localized = load_markdown_companion(
            path,
            base_fields=frontmatter,
            base_body=parts[2].strip(),
            localizable_fields=("description",),
        )
        if localized.warning:
            logger.warning(localized.warning)

        # Operational fields come from the raw frontmatter; only `description`
        # is overlaid from the localized companion.
        fm = frontmatter
        description = str(localized.fields.get("description") or "")
        mode = str(fm.get("mode") or "inline").strip()
        if mode not in _VALID_MODES:
            logger.warning(
                f"Environment source {name}: invalid mode {mode!r}, falling back to inline"
            )
            mode = "inline"

        trigger = str(fm.get("schedule_trigger") or "periodic").strip()
        if trigger not in _VALID_TRIGGERS:
            logger.warning(
                f"Environment source {name}: invalid schedule_trigger {trigger!r}, "
                f"falling back to periodic"
            )
            trigger = "periodic"

        params = fm.get("params") or {}
        if not isinstance(params, dict):
            logger.warning(
                f"Environment source {name}: params must be a mapping, ignoring"
            )
            params = {}

        definition = EnvironmentSourceDef(
            name=name,
            description=description,
            mode=mode,  # type: ignore[arg-type]
            language=str(fm.get("language") or "python").strip(),
            script=str(fm.get("script") or "source.py").strip(),
            enabled=bool(fm.get("enabled", True)),
            protected=bool(fm.get("protected", False)),
            schedule_trigger=trigger,  # type: ignore[arg-type]
            interval_seconds=float(fm.get("interval_seconds") or 0.0),
            every_seconds=int(fm.get("every_seconds") or 0),
            every_n_cycles=int(fm.get("every_n_cycles") or 0),
            every_n_tool_calls=int(fm.get("every_n_tool_calls") or 0),
            cold_floor_seconds=int(fm.get("cold_floor_seconds") or 0),
            min_interval_seconds=int(fm.get("min_interval_seconds") or 0),
            cron=str(fm.get("cron") or "").strip(),
            timeout_seconds=float(fm.get("timeout_seconds") or 60.0),
            params=dict(params),
            source_dir=str(path.parent),
        )
        return definition, localized.warning

    def _refresh_warnings(self, warnings: dict[str, str]) -> None:
        self._pending_warnings = [
            message
            for key, message in warnings.items()
            if self._active_warnings.get(key) != message
        ]
        self._active_warnings = warnings

    def consume_load_warnings(self) -> list[str]:
        warnings = list(self._pending_warnings)
        self._pending_warnings.clear()
        return warnings

    def get(self, name: str) -> EnvironmentSourceDef | None:
        return self._sources.get(name)

    def list_all(self) -> list[EnvironmentSourceDef]:
        return list(self._sources.values())

    def list_enabled(self) -> list[EnvironmentSourceDef]:
        return [src for src in self._sources.values() if src.enabled]
