"""One-time model-ID migrations for provider catalogs.

Providers occasionally replace a public model ID without keeping the old name
as an alias.  The refresh that introduced ``deepseek-v4.1-flash`` removed the
older OpenCode Go IDs, but existing installations may still reference them in
startup, summary, vision, fallback, or Provider ``default_model`` settings.

These migrations are provider-scoped: the official DeepSeek Provider still
serves ``deepseek-flash`` and must not be rewritten.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from coworker.core.config import Config, LLMConfig

# Legacy model IDs per provider type.  Only IDs that the provider no longer
# lists are included; the replacement ID must already be in its static catalog.
LEGACY_MODEL_ALIASES: dict[str, dict[str, str]] = {
    "opencode-go": {
        "deepseek-flash": "deepseek-v4.1-flash",
        "deepseek-v4-flash": "deepseek-v4.1-flash",
        "deepseek-v4-flash-vision-exp": "deepseek-v4.1-flash",
    },
}


@dataclass(frozen=True)
class ModelMigration:
    """One rewritten model reference, used for logging and tests."""

    scope: str
    provider: str
    old: str
    new: str


def migrate_model_id(provider_type: str, model_id: str) -> str:
    """Return the replacement ID for a legacy provider/model pair."""

    if not provider_type or not model_id:
        return model_id
    return LEGACY_MODEL_ALIASES.get(provider_type, {}).get(model_id, model_id)


def _provider_type(provider_name: str, types: dict[str, str]) -> str:
    if not provider_name:
        return ""
    known = types.get(provider_name)
    if known:
        return known
    # A provider name may equal its type when the flat environment fields are
    # used without a named ``providers.json`` entry (for example with a missing
    # API key, where no ProviderSpec is produced at all).
    return provider_name if provider_name in LEGACY_MODEL_ALIASES else ""


def _provider_types(llm: LLMConfig) -> dict[str, str]:
    return {spec.name: spec.type for spec in llm.resolved_providers()}


def migrate_model_configuration(
    config: Config,
    inherited: Config | None = None,
) -> list[ModelMigration]:
    """Migrate legacy model references in a loaded configuration.

    ``config`` is mutated in place.  When ``inherited`` is provided, the
    admin override and runtime model config files are rewritten so the
    migration survives future restarts.  Values inherited from ``.env`` cannot
    be rewritten safely and stay effective through this in-memory migration.
    """

    llm = config.llm
    changes: list[ModelMigration] = []

    for spec in llm.managed_providers:
        if not spec.default_model:
            continue
        new = migrate_model_id(spec.type, spec.default_model)
        if new != spec.default_model:
            changes.append(
                ModelMigration("provider", spec.name, spec.default_model, new)
            )
            spec.default_model = new

    types = _provider_types(llm)

    new = migrate_model_id(
        _provider_type(llm.default_provider, types),
        llm.default_model,
    )
    if new != llm.default_model:
        changes.append(
            ModelMigration("default", llm.default_provider, llm.default_model, new)
        )
        llm.default_model = new

    summary_provider = llm.summary_provider or llm.default_provider
    new = migrate_model_id(
        _provider_type(summary_provider, types),
        llm.summary_model,
    )
    if new != llm.summary_model:
        changes.append(
            ModelMigration("summary", summary_provider, llm.summary_model, new)
        )
        llm.summary_model = new

    if llm.vision_provider and llm.vision_model:
        new = migrate_model_id(
            _provider_type(llm.vision_provider, types),
            llm.vision_model,
        )
        if new != llm.vision_model:
            changes.append(
                ModelMigration("vision", llm.vision_provider, llm.vision_model, new)
            )
            llm.vision_model = new

    migrated_fallbacks: list[str] = []
    for entry in llm.fallbacks:
        name, separator, model = entry.partition("/")
        if separator and model:
            new = migrate_model_id(_provider_type(name, types), model)
            if new != model:
                changes.append(ModelMigration("fallback", name, model, new))
                entry = f"{name}/{new}"
        migrated_fallbacks.append(entry)
    llm.fallbacks = migrated_fallbacks

    if inherited is not None:
        _persist_admin_overrides(config, inherited, types)
        _persist_runtime_model_config(llm, types)

    return changes


def migrate_provider_model_reference(
    llm: LLMConfig,
    provider_name: str,
    model_id: str,
) -> str:
    """Migrate one model ID for a named provider (used for the snapshot)."""

    if not provider_name or not model_id:
        return model_id
    return migrate_model_id(_provider_type(provider_name, _provider_types(llm)), model_id)


def _persist_admin_overrides(
    config: Config,
    inherited: Config,
    types: dict[str, str],
) -> None:
    from coworker.core.config import (
        load_admin_overrides,
        sparse_admin_overrides,
        write_admin_overrides,
    )

    path = Path(config.admin.config_file)
    if not path.is_file():
        return
    try:
        overrides = load_admin_overrides(path)
        llm_overrides = overrides.get("llm")
        if not isinstance(llm_overrides, dict):
            return

        changed = False
        default_provider = str(
            llm_overrides.get("default_provider")
            or inherited.llm.default_provider
            or ""
        )
        for key, provider in (
            ("default_model", default_provider),
            (
                "summary_model",
                str(
                    llm_overrides.get("summary_provider")
                    or inherited.llm.summary_provider
                    or default_provider
                ),
            ),
            (
                "vision_model",
                str(
                    llm_overrides.get("vision_provider")
                    or inherited.llm.vision_provider
                    or ""
                ),
            ),
        ):
            value = llm_overrides.get(key)
            if not isinstance(value, str):
                continue
            new = migrate_model_id(_provider_type(provider, types), value)
            if new != value:
                llm_overrides[key] = new
                changed = True

        fallbacks = llm_overrides.get("fallbacks")
        if isinstance(fallbacks, list):
            migrated: list[Any] = []
            for entry in fallbacks:
                if not isinstance(entry, str):
                    migrated.append(entry)
                    continue
                name, separator, model = entry.partition("/")
                if separator and model:
                    new = migrate_model_id(_provider_type(name, types), model)
                    if new != model:
                        entry = f"{name}/{new}"
                        changed = True
                migrated.append(entry)
            llm_overrides["fallbacks"] = migrated

        managed = llm_overrides.get("managed_providers")
        if isinstance(managed, list):
            for item in managed:
                if not isinstance(item, dict):
                    continue
                value = item.get("default_model")
                if not isinstance(value, str):
                    continue
                provider_type = str(item.get("type") or "")
                new = migrate_model_id(provider_type, value)
                if new != value:
                    item["default_model"] = new
                    changed = True

        if changed:
            write_admin_overrides(path, sparse_admin_overrides(overrides, inherited))
    except OSError as error:
        logger.warning(f"Could not persist migrated model configuration to {path}: {error}")


def _persist_runtime_model_config(llm: LLMConfig, types: dict[str, str]) -> None:
    runtime_path = llm.runtime_config_file
    if not runtime_path:
        return
    path = Path(runtime_path)
    if not path.is_file():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning(f"Could not read runtime model configuration {path}: {error}")
        return
    if not isinstance(raw, dict):
        return

    changed = False
    summary = raw.get("summary")
    if isinstance(summary, dict) and isinstance(summary.get("model"), str):
        provider = str(summary.get("provider") or llm.summary_provider or llm.default_provider or "")
        new = migrate_model_id(_provider_type(provider, types), summary["model"])
        if new != summary["model"]:
            summary["model"] = new
            changed = True

    vision = raw.get("vision")
    if isinstance(vision, dict) and isinstance(vision.get("model"), str):
        provider = str(vision.get("provider") or llm.vision_provider or "")
        new = migrate_model_id(_provider_type(provider, types), vision["model"])
        if new != vision["model"]:
            vision["model"] = new
            changed = True

    fallbacks = raw.get("fallbacks")
    if isinstance(fallbacks, list):
        migrated: list[Any] = []
        for entry in fallbacks:
            if not isinstance(entry, str):
                migrated.append(entry)
                continue
            name, separator, model = entry.partition("/")
            if separator and model:
                new = migrate_model_id(_provider_type(name, types), model)
                if new != model:
                    entry = f"{name}/{new}"
                    changed = True
            migrated.append(entry)
        raw["fallbacks"] = migrated

    if not changed:
        return
    try:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as error:
        logger.warning(f"Could not persist migrated runtime model configuration {path}: {error}")
