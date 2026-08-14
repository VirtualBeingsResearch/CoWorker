"""Configuration lifecycle for Coworker administration."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from pydantic import ValidationError

from coworker.core.config import (
    Config,
    _deep_merge,
    load_admin_overrides,
    sparse_admin_overrides,
    write_admin_overrides,
)
from coworker.desktop_updates import build_runtime_spec
from coworker.i18n import tr

if TYPE_CHECKING:
    from coworker.agent.loop import AgentLoop
    from coworker.brain.brain import Brain
    from coworker.channels.module import ChannelModuleRegistry, ChannelSettings
    from coworker.desktop_updates import SyncService
    from coworker.memory.long_term import LongTermMemory

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

SECRET_PATHS = {
    "admin.token",
    "api.communication_token",
    "relay.instance_private_key",
    "desktop_updates.admin_token",
    "desktop_updates.feed_token",
    "llm.anthropic_api_key",
    "llm.openai_api_key",
    "llm.deepseek_api_key",
    "llm.qwen_api_key",
    "llm.zhipu_api_key",
    "llm.minimax_api_key",
    "wecom.secret",
}

MEM0_LLM_CONFIG_PATHS = {
    "memory.mem0_llm_provider",
    "memory.mem0_llm_model",
    "memory.mem0_llm_thinking",
}

HOT_CONFIG_PATHS = {
    "llm.max_tokens",
    "llm.model_prices",
    "agent.idle_sleep_seconds",
    "agent.passive_mode",
    "agent.inbox_batch_max",
    "agent.bubble_max_concurrent",
    "memory.auto_recall_enabled",
    "memory.auto_recall_relevance_threshold",
    "memory.auto_recall_limit",
} | MEM0_LLM_CONFIG_PATHS

_SOURCE_TOKEN_PATH_RE = re.compile(
    r"^desktop_updates\.sync_sources\.([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.token$"
)
_MANAGED_PROVIDER_SECRET_RE = re.compile(r"llm\.managed_providers\.\d+\.api_key")
_TELEGRAM_BOT_SECRET_RE = re.compile(
    r"telegram\.bots\.([a-z][a-z0-9_-]{0,31})\.bot_token"
)
_PROVIDER_REMOVAL_REASON = "llm.managed_providers.removed"


class SecretStatus(TypedDict):
    configured: bool
    last4: str


@dataclass(frozen=True)
class AdminConfigDependencies:
    agent: AgentLoop
    brain: Brain
    config: Config
    inherited_config: Config
    desktop_update_sync: SyncService | None = None
    long_term: LongTermMemory | None = None


@dataclass(frozen=True)
class ConfigUpdate:
    changes: JsonObject = field(default_factory=dict)
    secrets: dict[str, str | None] = field(default_factory=dict)
    clear_overrides: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ConfigApplyResult:
    applied_now: list[str]
    requires_restart: list[str]
    override_path: Path
    pending_restart: bool


@dataclass(frozen=True)
class ConfigSnapshot:
    config: JsonObject
    secret_status: dict[str, SecretStatus]
    effective_providers: list[JsonObject]
    overridden_fields: list[str]
    hot_reloadable: list[str]
    override_path: str
    pending_restart: bool


class ConfigUpdateError(Exception):
    def __init__(self, status_code: int, detail: object) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class AdminConfigService:
    """Own the desired, running, and pending-restart configuration lifecycle."""

    def __init__(self, dependencies: AdminConfigDependencies) -> None:
        self._dependencies = dependencies
        self._channel_modules: ChannelModuleRegistry | None = None
        self._pending_restart_reasons: set[str] = set()
        self.lock = asyncio.Lock()

    @property
    def pending_restart(self) -> bool:
        return bool(self._pending_restart_reasons)

    def set_channel_modules(self, modules: ChannelModuleRegistry | None) -> None:
        self._channel_modules = modules

    def mark_restart_pending(self, reason: str) -> None:
        self._pending_restart_reasons.add(reason)

    def clear_restart_pending(self, reason: str) -> None:
        self._pending_restart_reasons.discard(reason)

    def merge_overrides(self, overrides: JsonObject, changes: JsonObject) -> JsonObject:
        merged = dict(overrides)
        for section, section_changes in changes.items():
            current_section = merged.get(section)
            if isinstance(current_section, dict) and isinstance(section_changes, dict):
                merged[section] = {**current_section, **section_changes}
            else:
                merged[section] = section_changes
        return merged

    def write_sparse_overrides(self, path: Path, overrides: JsonObject) -> None:
        sparse = sparse_admin_overrides(overrides, self._dependencies.inherited_config)
        write_admin_overrides(path, cast(JsonObject, sparse))

    def prepare_overrides(
        self,
        current_overrides: JsonObject,
        update: ConfigUpdate,
    ) -> JsonObject:
        """Build sanitized overrides without applying them to the running process."""

        return self._prepare_overrides(current_overrides, update)

    def snapshot(self) -> ConfigSnapshot:
        data, statuses, effective_providers = self._masked_config()
        config = self._dependencies.config
        overrides = load_admin_overrides(config.admin.config_file)
        return ConfigSnapshot(
            config=data,
            secret_status=statuses,
            effective_providers=effective_providers,
            overridden_fields=self._overridden_fields(overrides),
            hot_reloadable=sorted(
                HOT_CONFIG_PATHS
                | {"llm.managed_providers", "desktop_updates", "channel_access"}
                | (
                    self._channel_modules.hot_reloadable_keys()
                    if self._channel_modules is not None
                    else set()
                )
            ),
            override_path=config.admin.config_file,
            pending_restart=self.pending_restart,
        )

    async def patch(self, update: ConfigUpdate) -> ConfigApplyResult:
        async with self.lock:
            if self.pending_restart and self._dependencies.brain.active_provider is None:
                raise ConfigUpdateError(409, tr("api.admin.already_initialized"))
            return await self._patch_locked(update)

    async def _patch_locked(self, update: ConfigUpdate) -> ConfigApplyResult:
        config = self._dependencies.config
        override_path = Path(config.admin.config_file)
        current_overrides = load_admin_overrides(override_path)
        next_overrides = self._prepare_overrides(current_overrides, update)
        before_config, desired_config = self._validated_configs(
            current_overrides,
            next_overrides,
        )
        llm_overrides = next_overrides.get("llm")
        if isinstance(llm_overrides, dict) and "model_prices" in llm_overrides:
            llm_overrides["model_prices"] = [
                price.model_dump(mode="json") for price in desired_config.llm.model_prices
            ]
        changed_paths = _changed_paths(
            before_config.model_dump(mode="json"),
            desired_config.model_dump(mode="json"),
        )
        try:
            applied_now, restart_reasons = await self._apply_hot_config(
                desired_config,
                changed_paths,
            )
        except Exception as error:
            raise ConfigUpdateError(
                400,
                tr("api.admin.runtime_apply_failed", error=error),
            ) from error

        self.write_sparse_overrides(override_path, next_overrides)
        requires_restart = self._refresh_pending_restart(
            desired_config,
            changed_paths,
            restart_reasons,
        )
        return ConfigApplyResult(
            applied_now=applied_now,
            requires_restart=requires_restart,
            override_path=override_path,
            pending_restart=self.pending_restart,
        )

    def _prepare_overrides(
        self,
        current_overrides: JsonObject,
        update: ConfigUpdate,
    ) -> JsonObject:
        safe_changes = json.loads(json.dumps(update.changes))
        for secret_path in SECRET_PATHS:
            _remove_path(safe_changes, secret_path)
        _remove_source_tokens(safe_changes)
        _remove_telegram_bot_tokens(safe_changes)
        _preserve_telegram_bot_tokens(safe_changes, current_overrides)

        next_overrides = dict(current_overrides)
        for clear_path in update.clear_overrides:
            if not self._config_field_exists(clear_path):
                raise ConfigUpdateError(
                    400,
                    tr("api.admin.config_field_not_clearable", path=clear_path),
                )
            _remove_path(next_overrides, clear_path)
        next_overrides = self.merge_overrides(next_overrides, safe_changes)
        self._merge_secrets(next_overrides, update.secrets)
        return next_overrides

    def _merge_secrets(
        self,
        overrides: JsonObject,
        secrets: dict[str, str | None],
    ) -> None:
        effective = self._dependencies.config.model_dump(mode="json")
        explicit_source_ids: set[str] = set()
        for secret_path, value in secrets.items():
            source_match = _SOURCE_TOKEN_PATH_RE.fullmatch(secret_path)
            if source_match is not None:
                self._set_desktop_source_secret(
                    overrides,
                    effective,
                    source_match.group(1).lower(),
                    value or "",
                )
                explicit_source_ids.add(source_match.group(1).lower())
                continue
            if (
                secret_path not in SECRET_PATHS
                and not _MANAGED_PROVIDER_SECRET_RE.fullmatch(secret_path)
                and not _TELEGRAM_BOT_SECRET_RE.fullmatch(secret_path)
            ):
                raise ConfigUpdateError(
                    400,
                    tr("api.admin.secret_not_writable", path=secret_path),
                )
            _set_path(overrides, secret_path, value or "")

        self._preserve_desktop_source_secrets(overrides, explicit_source_ids)
        self._preserve_provider_secrets(overrides)

    def _set_desktop_source_secret(
        self,
        overrides: JsonObject,
        effective: JsonObject,
        source_id: str,
        value: str,
    ) -> None:
        if not isinstance(_get_path(overrides, "desktop_updates.sync_sources"), list):
            _set_path(
                overrides,
                "desktop_updates.sync_sources",
                _get_path(effective, "desktop_updates.sync_sources", []),
            )
        try:
            _set_source_token(overrides, source_id, value)
        except ValueError as error:
            raise ConfigUpdateError(400, str(error)) from error

    def _preserve_desktop_source_secrets(
        self,
        overrides: JsonObject,
        explicit_source_ids: set[str],
    ) -> None:
        desired_sources = _get_path(overrides, "desktop_updates.sync_sources")
        if not isinstance(desired_sources, list):
            return
        old_sources = {
            str(source.id).lower(): source
            for source in self._dependencies.config.desktop_updates.sync_sources
        }
        valid_ids: set[str] = set()
        for item in desired_sources:
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("id") or "").lower()
            if not source_id:
                continue
            valid_ids.add(source_id)
            old_source = old_sources.get(source_id)
            should_preserve = (
                source_id not in explicit_source_ids
                and old_source is not None
                and str(item.get("type") or "") == old_source.type
                and not item.get("token")
            )
            if should_preserve:
                assert old_source is not None
                item["token"] = old_source.token
        _delete_removed_source_tokens(overrides, valid_ids)

    def _preserve_provider_secrets(self, overrides: JsonObject) -> None:
        managed = _get_path(overrides, "llm.managed_providers")
        if not isinstance(managed, list):
            return
        old_providers = {
            provider.name: provider for provider in self._dependencies.config.llm.managed_providers
        }
        for item in managed:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str) and not item.get("api_key") and name in old_providers:
                item["api_key"] = old_providers[name].api_key

    def _validated_configs(
        self,
        current_overrides: JsonObject,
        next_overrides: JsonObject,
    ) -> tuple[Config, Config]:
        effective = self._dependencies.config.model_dump(mode="json")
        desired_base = self._dependencies.config.model_dump(mode="json")
        inherited = self._dependencies.inherited_config.model_dump(mode="json")
        # A removed dynamic channel override must fall back to the inherited
        # mapping instead of being reintroduced from the already-hot-applied
        # running Config.
        desired_base["channel_access"] = inherited["channel_access"]
        desired_base["telegram"] = inherited["telegram"]
        try:
            before = Config.model_validate(_deep_merge(effective, current_overrides))
            desired = Config.model_validate(_deep_merge(desired_base, next_overrides))
        except ValidationError as error:
            raise ConfigUpdateError(422, json.loads(error.json())) from error
        return before, desired

    async def _apply_hot_config(
        self,
        desired: Config,
        changed_paths: set[str],
    ) -> tuple[list[str], set[str]]:
        applied: list[str] = []
        restart = self._restart_config_paths(changed_paths)
        await self._apply_provider_changes(desired, changed_paths, applied, restart)
        await self._apply_desktop_changes(desired, changed_paths, applied)
        await self._apply_channel_changes(desired, changed_paths, applied)
        self._apply_channel_access_changes(desired, changed_paths, applied)
        self._apply_scalar_changes(desired, changed_paths, applied)
        await self._apply_mem0_llm_changes(desired, changed_paths, applied)
        return sorted(set(applied)), restart

    async def _apply_mem0_llm_changes(
        self,
        desired: Config,
        changed_paths: set[str],
        applied: list[str],
    ) -> None:
        """mem0 LLM 配置（provider/model/thinking）变更时热替换 mem0 的 LLM 实例。

        未初始化或未注入 long_term 依赖时只把变更落到覆盖配置，待下次初始化生效。
        """
        changed = changed_paths & MEM0_LLM_CONFIG_PATHS
        if not changed:
            return
        from coworker.memory.long_term import build_memory_llm_config

        long_term = self._dependencies.long_term
        brain = self._dependencies.brain
        current_memory = self._dependencies.config.memory
        previous = (
            current_memory.mem0_llm_provider,
            current_memory.mem0_llm_model,
            current_memory.mem0_llm_thinking,
        )
        current_memory.mem0_llm_provider = desired.memory.mem0_llm_provider
        current_memory.mem0_llm_model = desired.memory.mem0_llm_model
        current_memory.mem0_llm_thinking = desired.memory.mem0_llm_thinking
        try:
            if long_term is not None:
                # reconfigure 在未初始化（setup 模式）时只记录配置，待 initialize 生效。
                # 跟随运行态主线：mem0 未显式配置时用当前 active provider/model，而非启动默认值。
                await long_term.reconfigure(
                    build_memory_llm_config(
                        desired,
                        active_provider=brain.current_provider_name,
                        active_model=brain.current_model,
                    )
                )
        except Exception:
            (
                current_memory.mem0_llm_provider,
                current_memory.mem0_llm_model,
                current_memory.mem0_llm_thinking,
            ) = previous
            raise
        for path in sorted(changed):
            applied.append(path)

    async def _apply_provider_changes(
        self,
        desired: Config,
        changed_paths: set[str],
        applied: list[str],
        restart: set[str],
    ) -> None:
        current = self._dependencies.config
        brain = self._dependencies.brain
        if "llm.max_tokens" in changed_paths:
            brain.set_max_tokens(desired.llm.max_tokens)
            current.llm.max_tokens = desired.llm.max_tokens
            applied.append("llm.max_tokens")

        if not any(path.startswith("llm.managed_providers") for path in changed_paths):
            return
        from coworker.brain.factory import build_provider

        current_specs = {spec.name: spec for spec in current.llm.resolved_providers()}
        desired_specs = {spec.name: spec for spec in desired.llm.resolved_providers()}
        changed_names = {
            name
            for name in current_specs.keys() | desired_specs.keys()
            if current_specs.get(name) != desired_specs.get(name)
        }
        for name, spec in desired_specs.items():
            if name not in changed_names:
                continue
            provider = build_provider(
                spec.type,
                spec.api_key,
                base_url=spec.base_url or None,
                name=spec.name,
                default_model=spec.default_model,
                tool_use_models=spec.tool_use_models,
                model_capabilities=spec.model_capabilities,
            )
            await brain.upsert_provider(provider)
        if current_specs.keys() - desired_specs.keys():
            restart.add(_PROVIDER_REMOVAL_REASON)
        current.llm.managed_providers = list(desired.llm.managed_providers)
        applied.append("llm.managed_providers")

    async def _apply_desktop_changes(
        self,
        desired: Config,
        changed_paths: set[str],
        applied: list[str],
    ) -> None:
        if not any(_is_desktop_hot(path) for path in changed_paths):
            return
        self._dependencies.config.desktop_updates = desired.desktop_updates
        sync = self._dependencies.desktop_update_sync
        if sync is not None:
            await sync.reconfigure(build_runtime_spec(desired.desktop_updates))
        from coworker.api import app as api_app

        api_app.setup_desktop_updates(desired.desktop_updates, desired.admin.token)
        applied.append("desktop_updates")

    async def _apply_channel_changes(
        self,
        desired: Config,
        changed_paths: set[str],
        applied: list[str],
    ) -> None:
        for _, settings in self._channel_settings():
            if not any(path.startswith(f"{settings.config_key}.") for path in changed_paths):
                continue
            desired_settings = getattr(desired, settings.config_key)
            await settings.apply(desired_settings)
            setattr(self._dependencies.config, settings.config_key, desired_settings)
            applied.append(settings.config_key)

    def _apply_channel_access_changes(
        self,
        desired: Config,
        changed_paths: set[str],
        applied: list[str],
    ) -> None:
        if not any(
            path == "channel_access" or path.startswith("channel_access.")
            for path in changed_paths
        ):
            return
        # ChannelAccessController retains this root model instance. Replacing its
        # mapping makes the new immutable rule objects visible to every Channel
        # without re-registering transports or restarting their runtimes.
        self._dependencies.config.channel_access.root = (
            desired.channel_access.model_copy(deep=True).root
        )
        applied.append("channel_access")

    def _apply_scalar_changes(
        self,
        desired: Config,
        changed_paths: set[str],
        applied: list[str],
    ) -> None:
        scalar_paths = changed_paths & (
            HOT_CONFIG_PATHS - {"llm.max_tokens"} - MEM0_LLM_CONFIG_PATHS
        )
        for path in sorted(scalar_paths):
            _assign_config_path(self._dependencies.config, path, desired)
            applied.append(path)
            if path == "agent.bubble_max_concurrent":
                store = getattr(self._dependencies.agent, "_bubble_store", None)
                if store is not None:
                    store.max_concurrent = desired.agent.bubble_max_concurrent

    def _refresh_pending_restart(
        self,
        desired: Config,
        changed_paths: set[str],
        apply_restart_reasons: set[str],
    ) -> list[str]:
        provider_changed = any(
            path.startswith("llm.managed_providers") for path in changed_paths
        )
        if provider_changed:
            self._pending_restart_reasons.discard(_PROVIDER_REMOVAL_REASON)
            if _PROVIDER_REMOVAL_REASON in apply_restart_reasons:
                self._pending_restart_reasons.add(_PROVIDER_REMOVAL_REASON)

        running_diff = _changed_paths(
            self._dependencies.config.model_dump(mode="json"),
            desired.model_dump(mode="json"),
        )
        pending_config_paths = self._restart_config_paths(running_diff)
        operational_reasons = self._pending_restart_reasons & {_PROVIDER_REMOVAL_REASON}
        self._pending_restart_reasons = pending_config_paths | operational_reasons
        return sorted(apply_restart_reasons & self._pending_restart_reasons)

    def _restart_config_paths(self, changed_paths: set[str]) -> set[str]:
        channel_prefixes = tuple(
            f"{settings.config_key}." for _, settings in self._channel_settings()
        )
        return {
            path
            for path in changed_paths
            if path not in HOT_CONFIG_PATHS
            and not _is_desktop_hot(path)
            and not path.startswith("llm.managed_providers")
            and not path.startswith(channel_prefixes)
            and not path.startswith("channel_access.")
            and path != "channel_access"
        }

    def _channel_settings(self) -> list[tuple[str, ChannelSettings]]:
        if self._channel_modules is None:
            return []
        return list(self._channel_modules.settings_items())

    def _config_field_exists(self, path: str) -> bool:
        parts = path.split(".")
        if len(parts) != 2:
            return False
        section, field_name = parts
        inherited = self._dependencies.inherited_config.model_dump(mode="json")
        section_data = inherited.get(section)
        if section == "channel_access":
            return bool(field_name.strip())
        return isinstance(section_data, dict) and field_name in section_data

    def _masked_config(
        self,
    ) -> tuple[JsonObject, dict[str, SecretStatus], list[JsonObject]]:
        desired_data = _deep_merge(
            self._dependencies.inherited_config.model_dump(mode="json"),
            load_admin_overrides(self._dependencies.config.admin.config_file),
        )
        desired = Config.model_validate(desired_data)
        data: JsonObject = desired.model_dump(mode="json")
        statuses = _mask_config_secrets(data)
        effective_providers = _effective_provider_snapshots(desired, statuses)
        return data, statuses, effective_providers

    @staticmethod
    def _overridden_fields(overrides: JsonObject) -> list[str]:
        fields: list[str] = []
        for section, section_overrides in overrides.items():
            if not isinstance(section_overrides, dict):
                fields.append(section)
                continue
            fields.extend(f"{section}.{field_name}" for field_name in section_overrides)
        return sorted(fields)


def _mask_config_secrets(data: JsonObject) -> dict[str, SecretStatus]:
    statuses: dict[str, SecretStatus] = {}
    for path in SECRET_PATHS:
        _mask_secret(data, path, statuses)

    desktop_updates = data.get("desktop_updates")
    sources = desktop_updates.get("sync_sources", []) if isinstance(desktop_updates, dict) else []
    if isinstance(sources, list):
        for item in sources:
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("id") or "")
            if source_id:
                _mask_secret(
                    item,
                    "token",
                    statuses,
                    f"desktop_updates.sync_sources.{source_id}.token",
                )

    llm = data.get("llm")
    providers = llm.get("managed_providers", []) if isinstance(llm, dict) else []
    if isinstance(providers, list):
        for index, provider in enumerate(providers):
            if isinstance(provider, dict):
                _mask_secret(
                    provider,
                    "api_key",
                    statuses,
                    f"llm.managed_providers.{index}.api_key",
                )

    telegram = data.get("telegram")
    bots = telegram.get("bots", {}) if isinstance(telegram, dict) else {}
    if isinstance(bots, dict):
        for instance_id, bot in bots.items():
            if isinstance(bot, dict):
                _mask_secret(
                    bot,
                    "bot_token",
                    statuses,
                    f"telegram.bots.{instance_id}.bot_token",
                )
    return statuses


def _effective_provider_snapshots(
    desired: Config,
    statuses: dict[str, SecretStatus],
) -> list[JsonObject]:
    managed_names = {spec.name for spec in desired.llm.managed_providers}
    providers: list[JsonObject] = []
    for index, spec in enumerate(desired.llm.resolved_providers()):
        provider = cast(JsonObject, spec.model_dump(mode="json"))
        value = str(provider.get("api_key", "") or "")
        statuses[f"effective_providers.{index}.api_key"] = _secret_status(value)
        provider["api_key"] = ""
        provider["managed"] = spec.name in managed_names
        providers.append(provider)
    return providers


def _mask_secret(
    data: JsonObject,
    path: str,
    statuses: dict[str, SecretStatus],
    status_path: str | None = None,
) -> None:
    value = str(_get_path(data, path, "") or "")
    statuses[status_path or path] = _secret_status(value)
    _set_path(data, path, "")


def _secret_status(value: str) -> SecretStatus:
    return {"configured": bool(value), "last4": value[-4:] if value else ""}


def _changed_paths(before: JsonValue, after: JsonValue, prefix: str = "") -> set[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        paths: set[str] = set()
        for key in before.keys() | after.keys():
            child = f"{prefix}.{key}" if prefix else key
            paths.update(_changed_paths(before.get(key), after.get(key), child))
        return paths
    if before != after:
        return {prefix}
    return set()


def _assign_config_path(config: Config, path: str, source: Config) -> None:
    group, field_name = path.split(".", 1)
    setattr(getattr(config, group), field_name, getattr(getattr(source, group), field_name))


def _is_desktop_hot(path: str) -> bool:
    return (
        path.startswith("desktop_updates.")
        and not path.startswith("desktop_updates.dir")
        and not path.startswith("desktop_updates.admin_token")
    )


def _set_path(data: JsonObject, dotted: str, value: JsonValue) -> None:
    parts = dotted.split(".")
    node: JsonValue = data
    for index, part in enumerate(parts[:-1]):
        if isinstance(node, dict):
            child = node.get(part)
            if child is None:
                child = [] if parts[index + 1].isdigit() else {}
                node[part] = child
        elif isinstance(node, list) and part.isdigit():
            item_index = int(part)
            if item_index >= len(node):
                raise ValueError(tr("api.admin.invalid_config_path", path=dotted))
            child = node[item_index]
        else:
            raise ValueError(tr("api.admin.invalid_config_path", path=dotted))
        node = child
    last = parts[-1]
    if isinstance(node, dict):
        node[last] = value
    elif isinstance(node, list) and last.isdigit() and int(last) < len(node):
        node[int(last)] = value
    else:
        raise ValueError(tr("api.admin.invalid_config_path", path=dotted))


def _get_path(data: JsonObject, dotted: str, default: JsonValue = None) -> JsonValue:
    node: JsonValue = data
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return default
    return node


def _remove_path(data: JsonObject, dotted: str) -> None:
    parts = dotted.split(".")
    node: JsonValue = data
    for part in parts[:-1]:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return
    if isinstance(node, dict):
        node.pop(parts[-1], None)
    elif isinstance(node, list) and parts[-1].isdigit() and int(parts[-1]) < len(node):
        node.pop(int(parts[-1]))


def _remove_source_tokens(data: JsonObject) -> None:
    desktop = data.get("desktop_updates")
    if not isinstance(desktop, dict):
        return
    sources = desktop.get("sync_sources")
    if isinstance(sources, list):
        for item in sources:
            if isinstance(item, dict):
                item.pop("token", None)


def _remove_telegram_bot_tokens(data: JsonObject) -> None:
    bots = _get_path(data, "telegram.bots")
    if not isinstance(bots, dict):
        return
    for bot in bots.values():
        if isinstance(bot, dict):
            bot.pop("bot_token", None)


def _preserve_telegram_bot_tokens(
    changes: JsonObject,
    current_overrides: JsonObject,
) -> None:
    """Retain only tokens already owned by the admin override file.

    Tokens inherited from ``.env`` remain inherited instead of being copied into
    ``admin_config.json`` when an administrator edits another Bot field.
    """

    changed_bots = _get_path(changes, "telegram.bots")
    current_bots = _get_path(current_overrides, "telegram.bots")
    if not isinstance(changed_bots, dict) or not isinstance(current_bots, dict):
        return
    for instance_id, changed_bot in changed_bots.items():
        current_bot = current_bots.get(instance_id)
        if not isinstance(changed_bot, dict) or not isinstance(current_bot, dict):
            continue
        token = current_bot.get("bot_token")
        if isinstance(token, str) and token:
            changed_bot["bot_token"] = token


def _set_source_token(data: JsonObject, source_id: str, value: str) -> None:
    desktop = data.setdefault("desktop_updates", {})
    if not isinstance(desktop, dict):
        raise ValueError("desktop_updates override must be an object")
    sources = desktop.get("sync_sources")
    if not isinstance(sources, list):
        raise ValueError("desktop_updates.sync_sources must be present before writing source token")
    for item in sources:
        if isinstance(item, dict) and str(item.get("id") or "").lower() == source_id:
            item["token"] = value
            return
    raise ValueError(f"desktop update source does not exist: {source_id}")


def _delete_removed_source_tokens(overrides: JsonObject, valid_ids: set[str]) -> None:
    sources = _get_path(overrides, "desktop_updates.sync_sources")
    if not isinstance(sources, list):
        return
    for item in sources:
        if isinstance(item, dict) and str(item.get("id") or "") not in valid_ids:
            item.pop("token", None)
