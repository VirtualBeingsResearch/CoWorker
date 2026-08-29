from __future__ import annotations

import asyncio
import json
import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from loguru import logger

from coworker.agent.bubble import BubbleStore
from coworker.agent.bubble_handoff import BubbleHandoffMatcher
from coworker.agent.bubble_router import BubbleMessageRouter
from coworker.agent.event_collector import RuntimeEventCollector
from coworker.agent.inbox_watcher import InboxWatcher
from coworker.agent.interaction_log import InteractionLogger
from coworker.agent.log_store import LogStore
from coworker.agent.loop import AgentLoop
from coworker.agent.subconscious import SubconsciousScheduler
from coworker.agent.subconscious_mode import SubconsciousModeLoader
from coworker.agent.usage_stats import UsageStatsCollector
from coworker.api import app as api_app
from coworker.api.admin import setup_admin, setup_channel_admin
from coworker.api.model_api import setup_model_api
from coworker.api.routes import setup as setup_routes
from coworker.brain.brain import Brain
from coworker.brain.factory import build_provider
from coworker.channels.modelapi import create_model_api_module
from coworker.channels.stream.desktop import (
    DesktopDispatcher,
    DesktopProfile,
    DesktopRegistry,
)
from coworker.channels.system import create_channel_system
from coworker.channels.telegram import TelegramModuleResources, create_telegram_module
from coworker.channels.wecom import WeComModuleResources, create_wecom_module
from coworker.channels.weixin import (
    WeixinModule,
    create_weixin_module,
)
from coworker.core.config import (
    Config,
    apply_admin_config_file,
    effective_admin_token,
    effective_communication_token,
    ensure_admin_token,
    normalize_admin_overrides_file,
)
from coworker.core.diagnostics import format_task_stacks, task_snapshot
from coworker.core.exceptions import ModelNotSupportedError, ProviderNotFoundError
from coworker.core.logging import intercept_standard_logging
from coworker.core.model_config import apply_runtime_model_config_file
from coworker.core.startup_intent import clear_startup_intent, load_bootstrap_startup_intent
from coworker.core.types import AgentState, IncomingEvent, Message
from coworker.desktop_updates import DesktopReleaseStore, SyncService, build_runtime_spec
from coworker.i18n import configure_locale, tr
from coworker.identity.identity import Identity
from coworker.memory.factory import (
    available_backends,
    build_long_term_backend,
    missing_backend_modules,
)
from coworker.memory.long_term import LongTermMemory, build_memory_llm_config
from coworker.memory.short_term import ShortTermMemory
from coworker.palaces.loader import PalaceLoader
from coworker.persona import PersonaCard, PersonaContext, PersonStore
from coworker.prompts.system_prompt import SystemPromptBuilder
from coworker.relay import RelayClient
from coworker.skills.loader import SkillLoader
from coworker.tools.alarm_tools import AlarmManager, CancelAlarmTool, ListAlarmsTool, SetAlarmTool
from coworker.tools.breathe_tool import BreatheTool
from coworker.tools.browser_tools import (
    BrowserActionTool,
    BrowserCloseTool,
    BrowserGetContentTool,
    BrowserListSessionsTool,
    BrowserOpenTool,
    BrowserScreenshotTool,
    BrowserSessionStore,
    BrowserViewTool,
)
from coworker.tools.bubble_tools import (
    BubbleCancelTool,
    BubbleCheckTool,
    BubbleDoneTool,
    BubbleListTool,
    BubbleSendTool,
    BubbleSpawnTool,
)
from coworker.tools.code_tools import (
    BackgroundJobStore,
    ExecuteCodeTool,
    GetCodeResultTool,
    KillCodeJobTool,
)
from coworker.tools.communicate_tool import CommunicateTool, ListConnectionTool
from coworker.tools.file_tools import (
    FindFilesTool,
    GrepFilesTool,
    ListDirectoryTool,
    ReadFileTool,
    WriteFileTool,
)
from coworker.tools.memory_tools import ManageMemoryTool, QueryMemoryTool
from coworker.tools.persona_tools import PersonaTool
from coworker.tools.pinned_context_tool import ManagePinnedContextTool
from coworker.tools.reasoning_tools import (
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskStore,
    TaskUpdateTool,
)
from coworker.tools.registry import ToolRegistry
from coworker.tools.skill_tools import GetSkillTool
from coworker.tools.system_tools import (
    ClearShortTermMemoryTool,
    GetContextTool,
    RestartSelfTool,
    SleepTool,
    SwitchModelTool,
    _validate_snapshot,
)
from coworker.tools.web_tools import FetchURLTool, SearchWebTool


def _setup_logging(logs_dir: str) -> None:
    Path(logs_dir).mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        colorize=True,
        format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}",
    )
    logger.add(
        f"{logs_dir}/coworker.log",
        rotation="10 MB",
        retention="7 days",
        level="DEBUG",
        encoding="utf-8",
    )
    intercept_standard_logging()


def _get_env_snapshot(*, runtime_locale: str | None = None) -> dict:
    snapshot: dict = {
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "os": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "cwd": os.getcwd(),
    }
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            snapshot["git_commit"] = r.stdout.strip()
    except Exception:
        pass
    if runtime_locale:
        snapshot["runtime_locale"] = runtime_locale
    return snapshot


def _diff_env(old: dict, new: dict) -> str | None:
    _LABELS = {
        "python_version": "startup.env_python_version",
        "python_executable": "startup.env_python_executable",
        "os": "startup.env_os",
        "machine": "startup.env_machine",
        "cwd": "startup.env_cwd",
        "git_commit": "startup.env_git_commit",
    }
    changes = []
    for key, label_key in _LABELS.items():
        ov, nv = old.get(key), new.get(key)
        if ov is not None and nv is not None and ov != nv:
            changes.append(tr("startup.env_item", label=tr(label_key), old=ov, new=nv))
    return (
        tr("startup.env_changed", changes=tr("startup.env_separator").join(changes))
        if changes
        else None
    )


def _diff_runtime_locale(old: dict, new: dict) -> str | None:
    old_locale = str(old.get("runtime_locale") or "")
    new_locale = str(new.get("runtime_locale") or "")
    if not old_locale or not new_locale or old_locale == new_locale:
        return None
    return tr("startup.locale_changed", old=old_locale, new=new_locale)


def _find_pending_tool_call(messages: list, tool_name: str) -> dict | None:
    """检查 primary 末尾是否有未完成的指定 tool call，返回 {id} 或 None。"""
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.role == "assistant" and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.get("function", {}).get("name") == tool_name:
                    tc_id = tc.get("id", "")
                    has_result = any(
                        m.role == "tool" and m.tool_call_id == tc_id for m in messages[i + 1 :]
                    )
                    if not has_result:
                        return {"id": tc_id}
            break
        elif msg.role in ("user", "system"):
            break
    return None


def _append_recovered_tool_result(
    short_term: ShortTermMemory,
    interaction_log: InteractionLogger,
    *,
    tool_name: str,
    content: str,
) -> bool:
    """Append a recovered tool result after restart and mirror it to ilog."""
    pending = _find_pending_tool_call(short_term.primary, tool_name)
    if not pending:
        return False

    short_term.primary.append(
        Message(
            role="tool",
            content=content,
            tool_call_id=pending["id"],
        )
    )
    interaction_log.log_tool_result(pending["id"], tool_name, content, is_error=False)
    return True


async def _enqueue_startup_event(
    inbox_watcher: InboxWatcher,
    event: IncomingEvent,
    *,
    passive_mode: bool,
) -> None:
    """Retain startup context without waking an agent in passive mode."""
    await inbox_watcher.push(event, wake=not passive_mode)


def _register_providers(brain: Brain, config: Config) -> None:
    for spec in config.llm.resolved_providers():
        if not spec.api_key.strip():
            logger.warning(f"Skipping LLM provider {spec.name!r}: API key is empty")
            continue
        try:
            brain.register_provider(
                build_provider(
                    spec.type,
                    spec.api_key,
                    base_url=spec.base_url or None,
                    name=spec.name,
                    default_model=spec.default_model,
                    tool_use_models=spec.tool_use_models,
                    model_capabilities=spec.model_capabilities,
                )
            )
        except ValueError as e:
            logger.error(tr("log.provider_skipped", provider=repr(spec.name), error=e))


def _load_config_layers() -> tuple[Config, Config]:
    inherited = Config()
    normalize_admin_overrides_file(inherited)
    config = apply_admin_config_file(inherited.model_copy(deep=True))
    configure_locale(config.i18n.locale)
    apply_runtime_model_config_file(config.llm)
    return inherited, config


def _load_config() -> Config:
    return _load_config_layers()[1]


async def _validate_model_runtime_config(brain: Brain, config: Config) -> None:
    await brain.update_model_config(
        thinking_effort=config.llm.thinking_effort,
        summary_provider=config.llm.summary_provider,
        summary_model=config.llm.summary_model,
        summary_thinking=config.llm.summary_thinking,
        summary_thinking_effort=config.llm.summary_thinking_effort,
        fallbacks=config.llm.fallbacks,
        vision_provider=config.llm.vision_provider,
        vision_model=config.llm.vision_model,
        vision_thinking=config.llm.vision_thinking,
        vision_thinking_effort=config.llm.vision_thinking_effort,
    )


def _bind_memory_model_following(
    brain: Brain,
    long_term: LongTermMemory,
    config: Config,
) -> None:
    """Keep mem0 on the active model while its provider remains implicit."""

    async def reconfigure_for_active_model(provider: str, model: str) -> None:
        if config.memory.mem0_llm_provider:
            return
        await long_term.reconfigure(
            build_memory_llm_config(
                config,
                active_provider=provider,
                active_model=model,
            )
        )

    brain.add_model_switch_listener(reconfigure_for_active_model)


def _validate_backend_available(config: Config) -> None:
    """确认配置的长期记忆后端在当前环境可用；否则抛带引导的本地化错误。

    这是“致命依赖”护栏：某后端缺依赖会让进程无法正常启动，因此在
    ``--check``（以及委托给它的 ``restart_self``）阶段就拦截，而不是等运行时
    裸 ``ImportError``。缺失时显式报错并给出安装引导，不静默降级。
    """
    configured = config.memory.backend
    if configured not in available_backends():
        missing = ", ".join(missing_backend_modules(configured)) or "mem0"
        raise RuntimeError(
            tr(
                "system.memory_backend_missing_deps",
                backend=configured,
                missing=missing,
            )
        )


async def _run_check() -> int:
    """--check 模式：走配置加载 + Provider 注册，不启动服务。0=通过，1=失败。"""
    try:
        config = _load_config()
        _validate_backend_available(config)
        _setup_logging(config.agent.logs_dir)
        brain = Brain(
            config.llm.default_provider,
            config.llm.default_model,
            message_time_prefix=config.agent.message_time_prefix,
            max_tokens=config.llm.max_tokens,
            fallbacks=config.llm.fallbacks,
            thinking_effort=config.llm.thinking_effort,
            summary_provider=config.llm.summary_provider,
            summary_model=config.llm.summary_model,
            summary_thinking=config.llm.summary_thinking,
            summary_thinking_effort=config.llm.summary_thinking_effort,
            vision_provider=config.llm.vision_provider,
            vision_model=config.llm.vision_model,
            vision_thinking=config.llm.vision_thinking,
            vision_thinking_effort=config.llm.vision_thinking_effort,
        )
        _register_providers(brain, config)
        await _validate_model_runtime_config(brain, config)
        identity = Identity(config.agent.identity_dir)
        identity.load()
        identity.detect_location()
        logger.info("[check] Environment OK")
        return 0
    except Exception as e:
        logger.error(f"[check] FAIL: {e}")
        return 1


def _build_stm_kwargs(config: Config, log_store: LogStore) -> dict:
    """ShortTermMemory 的构造参数（含记忆树配置），供主入口与回溯命令复用。"""
    return dict(
        max_tokens=config.memory.short_term_max_tokens,
        compress_ratio=config.memory.compress_ratio,
        log_store=log_store,
        tree_enabled=config.memory.tree_enabled,
        tree_spine_cap_fraction=config.memory.tree_spine_cap_fraction,
        tree_backfill_concurrency=config.memory.tree_backfill_concurrency,
        tree_merge_reach_depth=config.memory.tree_merge_reach_depth,
    )


async def _run_backfill() -> int:
    """--backfill-tree 模式：从原始日志全史一次性重建记忆树，写回快照后退出。0=成功。"""
    try:
        config = _load_config()
        _setup_logging(config.agent.logs_dir)
        log_store = LogStore(config.agent.logs_dir)
        snapshot_path = Path(config.memory.db_path) / "short_term_snapshot.json"
        stm_kwargs = _build_stm_kwargs(config, log_store)
        if snapshot_path.exists() and _validate_snapshot(snapshot_path):
            short_term = ShortTermMemory.load_from_file(snapshot_path, **stm_kwargs)
        else:
            short_term = ShortTermMemory(**stm_kwargs)

        brain = Brain(
            config.llm.default_provider,
            config.llm.default_model,
            message_time_prefix=config.agent.message_time_prefix,
            max_tokens=config.llm.max_tokens,
            fallbacks=config.llm.fallbacks,
            thinking_effort=config.llm.thinking_effort,
            summary_provider=config.llm.summary_provider,
            summary_model=config.llm.summary_model,
            summary_thinking=config.llm.summary_thinking,
            summary_thinking_effort=config.llm.summary_thinking_effort,
            vision_provider=config.llm.vision_provider,
            vision_model=config.llm.vision_model,
            vision_thinking=config.llm.vision_thinking,
            vision_thinking_effort=config.llm.vision_thinking_effort,
        )
        _register_providers(brain, config)
        await _validate_model_runtime_config(brain, config)
        interaction_log = InteractionLogger(
            f"{config.agent.logs_dir}/interactions.jsonl",
            rotation_bytes=config.agent.interaction_log_rotation_bytes,
        )
        brain.add_summary_usage_listener(
            lambda response, meta: interaction_log.log_summary_llm_response(
                provider=response.provider,
                model=response.model,
                usage=response.usage,
                context_hint=str(meta.get("context_hint") or ""),
                thinking=(
                    bool(meta.get("thinking"))
                    if meta.get("thinking") is not None
                    else None
                ),
                thinking_effort=str(meta.get("thinking_effort") or "") or None,
            )
        )
        if short_term.active_provider and short_term.active_model:
            try:
                await brain.switch_model(short_term.active_provider, short_term.active_model)
            except (ProviderNotFoundError, ModelNotSupportedError) as e:
                logger.warning(f"[backfill] Could not restore previous model: {e}")

        logger.info(tr("log.backfill_start"))
        n = await short_term.backfill_tree_from_log(
            brain, max_leaves=config.memory.tree_backfill_max_leaves
        )
        short_term.save_to_file(snapshot_path)
        logger.info(
            tr("log.backfill_complete", leaves=n, nodes=len(short_term.tree.nodes))
        )
        return 0
    except Exception as e:
        logger.error(f"[backfill] FAIL: {e}")
        return 1


def _print_setup_admin_token(config: Config) -> None:
    """Show the active setup credential without copying it into persistent logs."""

    token = effective_admin_token(config)
    if not token:
        return
    admin_url = (
        f"{config.api.public_url}/admin"
        if config.api.public_url
        else f"http://127.0.0.1:{config.api.port}/admin"
    )
    print(
        "\n"
        + "=" * 68
        + "\n"
        + tr(
            "cli.first_run_token",
            admin_url=admin_url,
            token=token,
        )
        + "\n"
        + "=" * 68
        + "\n",
        file=sys.stderr,
        flush=True,
    )


async def _main() -> bool:
    """主入口。返回 True 表示请求重启，由 run_sync() 交给平台 launcher 处理。"""
    inherited_config, config = _load_config_layers()
    api_app.setup_cors(config.api.cors_origins)
    _setup_logging(config.agent.logs_dir)
    ensure_admin_token(config)
    logger.info("Starting coworker")
    brain = Brain(
        config.llm.default_provider,
        config.llm.default_model,
        message_time_prefix=config.agent.message_time_prefix,
        max_tokens=config.llm.max_tokens,
        fallbacks=config.llm.fallbacks,
        thinking_effort=config.llm.thinking_effort,
        summary_provider=config.llm.summary_provider,
        summary_model=config.llm.summary_model,
        summary_thinking=config.llm.summary_thinking,
        summary_thinking_effort=config.llm.summary_thinking_effort,
        vision_provider=config.llm.vision_provider,
        vision_model=config.llm.vision_model,
        vision_thinking=config.llm.vision_thinking,
        vision_thinking_effort=config.llm.vision_thinking_effort,
    )
    _register_providers(brain, config)
    await _validate_model_runtime_config(brain, config)
    setup_required = brain.active_provider is None
    api_app.set_setup_required(setup_required)
    if setup_required:
        _print_setup_admin_token(config)
        config.agent.tick = False
        logger.warning("No active LLM provider; running in first-run setup mode")

    interaction_log = InteractionLogger(
        f"{config.agent.logs_dir}/interactions.jsonl",
        rotation_bytes=config.agent.interaction_log_rotation_bytes,
    )
    # 原始日志的只读寻址层，供记忆块树按时间区间重摘要 / 下钻；抗后续分片轮转。
    log_store = LogStore(config.agent.logs_dir)

    long_term = LongTermMemory(
        backend=build_long_term_backend(
            config,
            active_provider=brain.current_provider_name,
            active_model=brain.current_model,
        ),
    )
    if setup_required:
        Path(config.memory.db_path).mkdir(parents=True, exist_ok=True)
        logger.warning("Long-term memory initialization deferred until first-run setup completes")
    else:
        await long_term.initialize()
    _bind_memory_model_following(brain, long_term, config)

    snapshot_path = Path(config.memory.db_path) / "short_term_snapshot.json"
    alarm_persist_path = Path(config.memory.db_path) / "alarms.json"
    env_snapshot_path = Path(config.memory.db_path) / "env_snapshot.json"

    current_env = _get_env_snapshot(runtime_locale=config.i18n.locale.value)
    prev_env: dict = {}
    if env_snapshot_path.exists():
        try:
            prev_env = json.loads(env_snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    env_diff = _diff_env(prev_env, current_env) if prev_env else None
    locale_diff = _diff_runtime_locale(prev_env, current_env) if prev_env else None
    env_snapshot_path.write_text(
        json.dumps(current_env, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if env_diff:
        logger.info(f"Environment changed since last run: {env_diff}")

    startup_intent = load_bootstrap_startup_intent(
        config.memory.db_path,
        provider=config.llm.default_provider,
        model=config.llm.default_model,
        available_providers=set(brain.list_providers()),
    )
    api_app.setup_bootstrap_reconnect_proof(
        startup_intent.reconnect_proof if startup_intent is not None else ""
    )
    bootstrap_clean_start = startup_intent is not None
    if bootstrap_clean_start and snapshot_path.exists():
        snapshot_path.unlink()
        logger.info("Discarded pre-bootstrap short-term snapshot")

    # 快照自检：损坏则删除，降级为全新启动。首次初始化重启始终干净启动。
    snapshot_valid = (
        not bootstrap_clean_start
        and snapshot_path.exists()
        and _validate_snapshot(snapshot_path)
    )
    is_restart = snapshot_valid
    if snapshot_path.exists() and not snapshot_valid:
        snapshot_path.unlink()
        logger.warning("Corrupt snapshot deleted, starting fresh")

    stm_kwargs = _build_stm_kwargs(config, log_store)

    if is_restart:
        short_term = ShortTermMemory.load_from_file(snapshot_path, **stm_kwargs)

        # 检测并注入 restart_self / sleep 悬空 tool call 的结果（在 cleanup 之前，确保调用链完整）
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        if _find_pending_tool_call(short_term.primary, "restart_self"):
            alarm_count = 0
            if alarm_persist_path.exists():
                try:
                    alarm_count = len(json.loads(alarm_persist_path.read_text(encoding="utf-8")))
                except Exception:
                    pass
            content = tr(
                "startup.restart_tool_result",
                time=now_str,
                messages=len(short_term.primary),
                alarms=(tr("startup.alarm_fragment", count=alarm_count) if alarm_count else ""),
                environment=(
                    "".join(
                        tr("startup.environment_fragment", changes=change)
                        for change in (env_diff, locale_diff)
                        if change
                    )
                ),
            )
            _append_recovered_tool_result(
                short_term,
                interaction_log,
                tool_name="restart_self",
                content=content,
            )

        sleep_content = tr("startup.sleep_interrupted", time=now_str)
        _append_recovered_tool_result(
            short_term,
            interaction_log,
            tool_name="sleep",
            content=sleep_content,
        )

        removed = short_term.cleanup_incomplete_tool_calls()
        logger.info(
            f"Restored short-term memory: {len(short_term.primary)} messages"
            + (f" (cleaned {removed} dangling)" if removed else "")
        )
    else:
        short_term = ShortTermMemory(**stm_kwargs)

    if (
        short_term.active_provider
        and short_term.active_model
        and (
            short_term.active_provider != brain.current_provider_name
            or short_term.active_model != brain.current_model
        )
    ):
        try:
            await brain.switch_model(short_term.active_provider, short_term.active_model)
            logger.info(
                f"Restored model from snapshot: {short_term.active_provider}/{short_term.active_model}"
            )
        except (ProviderNotFoundError, ModelNotSupportedError) as e:
            logger.warning(
                f"Could not restore previous model ({short_term.active_provider}/{short_term.active_model}): {e}"
            )

    if is_restart:
        short_term.schedule_tree_rebalance_if_needed(brain, snapshot_path=snapshot_path)

    identity = Identity(config.agent.identity_dir)
    identity.load()
    identity.detect_location()

    skill_loader = SkillLoader(config.agent.skills_dir)
    palace_loader = PalaceLoader(config.agent.palaces_dir)
    palace_loader.load_all()

    mode_loader = SubconsciousModeLoader(config.agent.subconscious_dir)
    mode_loader.load_all()

    agent_state = AgentState(
        current_provider=brain.current_provider_name,
        current_model=brain.current_model,
        tick=config.agent.tick,
        setup_mode=setup_required,
    )

    # 运行日志采集器：作为 InteractionLogger 的唯一 tap，把每条日志条目实时扇出给
    # /api/logs/stream 的 SSE 订阅者（身份证背面运行日志的数据源）。复用 agent_state 的
    # 企微 ID→人名脱敏，但不把事件发射耦合回 state。
    event_collector = RuntimeEventCollector(log_store, redact=agent_state._replace_ids)
    usage_stats = UsageStatsCollector(
        log_store,
        state_path=Path(config.agent.logs_dir) / "usage_stats.json",
    )
    usage_stats.load_bubble_history(config.agent.logs_dir)
    interaction_log.add_listener(event_collector.on_entry)
    interaction_log.add_listener(usage_stats.on_entry)
    short_term.add_compression_listener(
        lambda event: interaction_log.log_memory_compression(**event)
    )

    def log_long_term_usage(entry: dict[str, Any]) -> None:
        usage = entry.get("usage")
        interaction_log.log_long_term_llm_response(
            provider=str(entry.get("provider") or "unknown"),
            model=str(entry.get("model") or "unknown"),
            usage=usage if isinstance(usage, dict) else {},
            usage_source=str(entry.get("usage_source") or ""),
            operation=str(entry.get("operation") or ""),
        )

    long_term.add_usage_listener(log_long_term_usage)
    brain.add_summary_usage_listener(
        lambda response, meta: interaction_log.log_summary_llm_response(
            provider=response.provider,
            model=response.model,
            usage=response.usage,
            context_hint=str(meta.get("context_hint") or ""),
            thinking=(
                bool(meta.get("thinking"))
                if meta.get("thinking") is not None
                else None
            ),
            thinking_effort=str(meta.get("thinking_effort") or "") or None,
        )
    )
    brain.add_vision_usage_listener(
        lambda response, meta: interaction_log.log_vision_llm_response(
            provider=response.provider,
            model=response.model,
            usage=response.usage,
            label=str(meta.get("label") or ""),
            thinking=(
                bool(meta.get("thinking"))
                if meta.get("thinking") is not None
                else None
            ),
            thinking_effort=str(meta.get("thinking_effort") or "") or None,
        )
    )

    inbox_watcher = InboxWatcher(config.agent.inbox_dir, config.agent.inbox_poll_interval)

    channel_system = create_channel_system(
        outbox_dir=config.agent.outbox_dir,
        activity_path=Path(config.memory.db_path) / "channel_activity.json",
        access_config=config.channel_access,
        traffic_path=Path(config.agent.logs_dir) / "channel_traffic.jsonl",
    )
    channel_system.registry.set_inbound_handler(inbox_watcher.push)
    weixin_module: WeixinModule | None = None
    if not setup_required:
        weixin_module = create_weixin_module(
            config.weixin,
            Path(config.memory.db_path),
            channel_system.activity,
        )
        channel_system.install(weixin_module)
        channel_system.install(
            create_telegram_module(
                config.telegram,
                TelegramModuleResources(
                    state_dir=Path(config.memory.db_path) / "telegram",
                    attachments_dir=Path(config.agent.inbox_dir).parent / "attachments",
                    activity=channel_system.activity,
                ),
            )
        )
    communicate = CommunicateTool(channel_system.registry)
    job_store = BackgroundJobStore()
    browser_store = BrowserSessionStore()
    registry = ToolRegistry()
    task_store = TaskStore("data/tasks.json")
    desktop_registry = DesktopRegistry(short_term, config.agent.desktop_registry_dir)
    short_term.unpin("codex_registry")

    desktop_dispatcher = DesktopDispatcher(desktop_registry)
    channel_system.stream_runtime.add_connection_listener(
        lambda: desktop_registry.update_connections(
            set(channel_system.stream_runtime.list_live_stream_participant_ids())
        )
    )
    desktop_registry.update_connections(
        set(channel_system.stream_runtime.list_live_stream_participant_ids())
    )
    registry.register_many(
        [
            TaskCreateTool(task_store),
            TaskGetTool(task_store),
            TaskListTool(task_store),
            TaskUpdateTool(task_store),
            ReadFileTool(),
            WriteFileTool(),
            ListDirectoryTool(),
            FindFilesTool(),
            GrepFilesTool(),
            SearchWebTool(),
            FetchURLTool(),
            BrowserOpenTool(browser_store),
            BrowserScreenshotTool(browser_store),
            BrowserActionTool(browser_store),
            BrowserGetContentTool(browser_store),
            BrowserCloseTool(browser_store),
            BrowserListSessionsTool(browser_store),
            BrowserViewTool(
                browser_store,
                max_dimension=config.agent.image_max_dimension,
            ),
            ExecuteCodeTool(
                store=job_store,
                hard_timeout=config.agent.code_hard_timeout,
                inbox=inbox_watcher,
            ),
            GetCodeResultTool(job_store, inbox=inbox_watcher),
            KillCodeJobTool(job_store),
            QueryMemoryTool(
                long_term,
                short_term,
                brain,
            ),
            ManageMemoryTool(long_term),
            SleepTool(inbox_watcher, config=config),
            BreatheTool(),
            SwitchModelTool(brain),
        ]
    )
    alarm_manager = AlarmManager(inbox_watcher, persist_path=alarm_persist_path)
    restored_alarms = await alarm_manager.restore()
    person_store: PersonStore | None = None
    persona_cards: PersonaCard | None = None
    persona_context: PersonaContext | None = None
    if config.memory.persona_enabled:
        person_store = PersonStore(config.memory.persona_store_path)
        persona_cards = PersonaCard()
        persona_context = PersonaContext(store=person_store, cards=persona_cards)
    registry.register_many(
        [
            SetAlarmTool(alarm_manager),
            ListAlarmsTool(alarm_manager),
            CancelAlarmTool(alarm_manager),
            communicate,
            ListConnectionTool(channel_system.registry),
            GetSkillTool(skill_loader, agent_state),
            GetContextTool(brain, short_term, agent_state),
            ManagePinnedContextTool(short_term),
            RestartSelfTool(short_term=short_term, snapshot_path=snapshot_path),
            *(  # 可选的 Person 子机制：绑定地址、维护画像、合并人物。
                [PersonaTool(person_store, persona_cards)]
                if person_store is not None and persona_cards is not None
                else []
            ),
        ]
    )

    from coworker.tools.vision_tools import ViewImageTool, VisualAnalysisTool

    registry.register_many(
        [
            VisualAnalysisTool(
                brain,
                inbox=inbox_watcher,
                max_dimension=config.agent.image_max_dimension,
            ),
            ViewImageTool(max_dimension=config.agent.image_max_dimension),
        ]
    )

    prompt_builder = SystemPromptBuilder(
        identity,
        registry,
        skill_loader,
        palace_loader=palace_loader,
        channel_registry=channel_system.registry,
        thinking_path="data/thinking.md",
        git_commit=current_env.get("git_commit"),
        system_prompt_template=config.agent.system_prompt_template,
    )

    bubble_store: BubbleStore | None = None
    if config.agent.bubble_thinking:
        bubble_store = BubbleStore(
            max_concurrent=config.agent.bubble_max_concurrent,
            timeout_resume_seconds=config.agent.bubble_timeout_resume_seconds,
        )
        bubble_spawn = BubbleSpawnTool(
            store=bubble_store,
            short_term=short_term,
            parent_brain=brain,
            full_registry=registry,
            system_prompt_builder=prompt_builder,
            inbox=inbox_watcher,
            logs_dir=config.agent.logs_dir,
            parent_log=interaction_log,
            usage_stats=usage_stats,
            palace_loader=palace_loader,
            skill_loader=skill_loader,
            long_term=long_term,
            communicate=communicate,
            stream_runtime=channel_system.stream_runtime,
            handoff_matcher=BubbleHandoffMatcher.from_config(
                participant_matches=(config.agent.bubble_handoff_transparency_participant_matches),
                stream_transports=(config.agent.bubble_handoff_transparency_stream_transports),
            ),
        )
        registry.register_many(
            [
                bubble_spawn,
                BubbleCheckTool(bubble_store),
                BubbleSendTool(bubble_store, inbox_watcher),
                BubbleCancelTool(bubble_store),
                BubbleListTool(bubble_store),
                BubbleDoneTool(),
            ]
        )

    subconscious: SubconsciousScheduler | None = None
    if config.agent.subconscious_thinking:
        if bubble_store is None:
            bubble_store = BubbleStore(
                max_concurrent=config.agent.bubble_max_concurrent,
                timeout_resume_seconds=config.agent.bubble_timeout_resume_seconds,
            )
        subconscious = SubconsciousScheduler(
            cfg=config,
            bubble_store=bubble_store,
            brain=brain,
            tool_registry=registry,
            prompt_builder=prompt_builder,
            short_term=short_term,
            inbox=inbox_watcher,
            logs_dir=config.agent.logs_dir,
            interaction_log=interaction_log,
            usage_stats=usage_stats,
            state_path=Path(config.memory.db_path) / "subconscious_state.json",
            task_store=task_store,
            palace_loader=palace_loader,
            long_term=long_term,
            mode_loader=mode_loader,
        )

    # Desktop envelopes are normalized by the first interceptor above.  The
    # bubble router then gets the clean inbound event and may hand it directly
    # to an explicitly participant-bound active bubble.
    if bubble_store is not None:
        inbox_watcher.add_interceptor(BubbleMessageRouter(bubble_store))
    registry.register(ClearShortTermMemoryTool(short_term, brain, subconscious))

    if not setup_required and is_restart:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        restart_msg = tr("startup.system_restarted", time=now_str)
        if restored_alarms:
            restart_msg += tr("startup.alarms_restored", count=restored_alarms)
        if env_diff:
            restart_msg += tr("startup.environment_fragment", changes=env_diff)
        if locale_diff:
            restart_msg += tr("startup.environment_fragment", changes=locale_diff)
        await _enqueue_startup_event(
            inbox_watcher,
            IncomingEvent(
                participant_id="system",
                content=restart_msg,
                source="system",
            ),
            passive_mode=config.agent.passive_mode,
        )
    elif not setup_required and (env_diff or locale_diff):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        await _enqueue_startup_event(
            inbox_watcher,
            IncomingEvent(
                participant_id="system",
                content=tr(
                    "startup.system_started",
                    time=now_str,
                    environment=tr("startup.env_separator").join(
                        change for change in (env_diff, locale_diff) if change
                    ),
                ),
                source="system",
            ),
            passive_mode=config.agent.passive_mode,
        )

    agent_loop = AgentLoop(
        brain=brain,
        short_term=short_term,
        long_term=long_term,
        tool_registry=registry,
        identity=identity,
        prompt_builder=prompt_builder,
        inbox_watcher=inbox_watcher,
        config=config,
        interaction_log=interaction_log,
        state=agent_state,
        snapshot_path=snapshot_path,
        task_store=task_store,
        bubble_store=bubble_store,
        subconscious=subconscious,
        persona=persona_context,
    )

    desktop_release_store = DesktopReleaseStore(config.desktop_updates.dir)
    desktop_update_runtime = build_runtime_spec(config.desktop_updates)
    desktop_update_sync = SyncService(
        desktop_release_store,
        desktop_update_runtime.source,
        interval_seconds=desktop_update_runtime.interval_seconds,
        enabled=desktop_update_runtime.enabled,
        ready=desktop_update_runtime.ready,
        readiness=desktop_update_runtime.readiness,
        source_summary=desktop_update_runtime.source_summary,
        runtime_key=desktop_update_runtime.runtime_key,
        token=desktop_update_runtime.token,
        auto_publish=desktop_update_runtime.auto_publish,
    )
    relay_client = RelayClient(api_app.app, config)

    if not setup_required:
        channel_system.install(
            create_wecom_module(
                config.wecom,
                WeComModuleResources(
                    attachments_dir=Path(config.agent.inbox_dir).parent / "attachments",
                    contacts_path=Path(config.memory.db_path) / "wecom_contacts.json",
                    activity=channel_system.activity,
                ),
            )
        )
    setup_routes(
        None if setup_required else inbox_watcher,
        agent_loop,
        brain,
        config.agent.inbox_dir,
        usage_stats,
        config.llm.runtime_config_file,
        effective_communication_token(config),
        channels=channel_system.registry,
        communication_token_explicit=bool(config.api.communication_token),
    )
    setup_admin(
        agent=agent_loop,
        brain=brain,
        config=config,
        alarm_manager=alarm_manager,
        skill_loader=skill_loader,
        palace_loader=palace_loader,
        mode_loader=mode_loader,
        desktop_update_sync=desktop_update_sync,
        inherited_config=inherited_config,
        long_term=long_term,
        relay_client=relay_client,
        person_store=person_store,
        persona_cards=persona_cards,
        usage_stats=usage_stats,
    )
    setup_channel_admin(channel_system.modules)
    api_app.setup_desktop_updates(
        config.desktop_updates,
        config.admin.token,
        desktop_release_store,
    )
    # After the channel system is wired (setup_channels below), auto-published
    # releases notify online desktops just like a manual publish would.
    desktop_update_sync.on_release_published = api_app.notify_desktop_update_published
    api_app.setup_channels(None if setup_required else channel_system)
    api_app.set_collector(event_collector)

    if not setup_required:
        channel_system.register_stream_profile(
            DesktopProfile(
                desktop_registry,
                desktop_dispatcher,
            )
        )

    if not setup_required:
        model_api_module = create_model_api_module(
            config.model_api,
            Path(config.memory.db_path) / "model_api_sessions.json",
        )
        channel_system.install(model_api_module)
        setup_model_api(
            channel=model_api_module.channel,
            runtime=model_api_module.runtime,
        )

    # 写入实例状态文件（新旧交接标记）
    status_path = Path(config.memory.db_path) / "instance_status.json"
    status_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "started_at": datetime.now().isoformat(),
                "is_restart": is_restart,
                "startup_reason": "bootstrap" if bootstrap_clean_start else "restart" if is_restart else "start",
                "setup_mode": setup_required,
                "agent_loop_started": not setup_required,
                "messages_restored": len(short_term.primary),
                "alarms_restored": restored_alarms,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if bootstrap_clean_start:
        clear_startup_intent(config.memory.db_path)
    logger.info(f"Instance ready (pid={os.getpid()}, is_restart={is_restart})")

    uv_config = uvicorn.Config(
        api_app.app,
        host=config.api.host,
        port=config.api.port,
        log_level="warning",
        # 用新的 sansio WebSocket 实现，避免默认 "auto" 走 websockets.legacy（已弃用、启动刷
        # DeprecationWarning）。sansio 是 websockets 14+ 的非 legacy 接口，行为等价。
        ws="websockets-sansio",
        # 放宽 WS 心跳：agent 思考时事件循环可能被同步逻辑短暂占用，
        # 默认 20s/20s 太敏感会误断。拉长 ping 间隔与超时容忍。
        ws_ping_interval=60.0,
        ws_ping_timeout=120.0,
        # 关键：限定优雅关闭时长。默认 None 会让 serve() 在 server.wait_closed() 上无限
        # 等待长连接（SSE/WS）关闭，导致重启时 server_task 永不结束。给 3s 上界，超时后
        # uvicorn 自行取消残留请求、serve() 返回，重启得以推进（正常路径靠 teardown 主动唤醒
        # 连接清零，这里只是兜底）。
        timeout_graceful_shutdown=3,
    )
    server = uvicorn.Server(uv_config)

    await desktop_update_sync.start(run_immediately=config.desktop_updates.sync_on_start)
    await channel_system.registry.start()
    await relay_client.start()
    server_task = asyncio.create_task(server.serve(), name="server")
    inbox_task: asyncio.Task | None = None
    loop_task: asyncio.Task | None = None
    if setup_required:
        lifecycle_task = asyncio.create_task(
            agent_loop.wait_until_stopped(), name="setup-waiter"
        )
    else:
        inbox_task = asyncio.create_task(inbox_watcher.start(), name="inbox")
        loop_task = asyncio.create_task(agent_loop.run(), name="loop")
        lifecycle_task = loop_task
    try:
        done, _ = await asyncio.wait(
            {server_task, lifecycle_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if server_task in done:
            agent_loop.stop()
            await lifecycle_task
            server_task.result()
        else:
            await lifecycle_task
    finally:
        reason = agent_state.restart_reason or (
            "restart" if agent_state.restart_requested else "shutdown"
        )
        logger.info(f"Teardown begin (reason={reason}); stopping server + background tasks")
        # 退出/重启前先拍一张事件循环快照：哪些 task 还活着、各自挂在哪个 await。
        # 这正是定位「卡着到不了退出那一步」的根因证据，无需复现即可在日志中留痕。
        pending_now = [t for t in task_snapshot() if not t["done"] and not t["current"]]
        logger.info(
            "Live tasks at teardown ({} pending): {}".format(
                len(pending_now),
                "; ".join(f"{t['name']}@{t['waiting_at']}" for t in pending_now),
            )
        )
        # 聊天 web UI 维持长连接（/sse/{id} SSE 流 + /ws）。这些流式响应阻塞在 queue.get()，
        # 只在循环顶部检查 is_disconnected()，关闭时不会自行结束。uvicorn 的 serve() 收尾时要
        # await server.wait_closed()——Python 3.13 下它会等到这些连接关闭为止，而
        # timeout_graceful_shutdown 默认 None = 无限等待，于是 server_task 永不结束、进程卡死
        # 到不了平台重启交接。
        #
        # 对策：主动唤醒 communicate/pool 的 /sse、/ws 出站队列，让它们立即跳出循环、释放连接
        # → 连接数归零 → wait_closed() 立刻返回。连接清零后无需 force_exit，uvicorn 能正常走
        # lifespan.shutdown（force_exit 会跳过它，反而导致 lifespan 任务被取消、刷 CancelledError
        # 噪声）。timeout_graceful_shutdown=3 仅作兜底，正常路径用不到。
        api_app.signal_shutdown()
        await channel_system.registry.stop()
        await relay_client.stop()
        server.should_exit = True
        if inbox_task is not None:
            inbox_watcher.stop()
        await desktop_update_sync.stop()
        logger.info("Desktop update sync stopped")
        background = [server_task]
        if inbox_task is not None:
            background.append(inbox_task)
        if not lifecycle_task.done():
            agent_loop.stop()
            background.append(lifecycle_task)
        # 用 asyncio.wait（不取消 pending，便于如实记录谁卡住），超时后再点名 + 打栈 + 取消。
        _, pending = await asyncio.wait(background, timeout=10)
        if pending:
            logger.warning(
                tr(
                    "log.shutdown_tasks_stuck",
                    tasks=", ".join(t.get_name() for t in pending),
                )
            )
            # 把卡住 task 的挂起栈写进日志——直接定位「卡在哪个 await」的根因。
            logger.warning("Stuck task stacks:\n" + format_task_stacks(list(pending)))
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            logger.info("Background tasks force-cancelled")
        else:
            logger.info(f"All background tasks stopped cleanly ({len(background)})")
        await browser_store.stop()
        logger.info("Browser store stopped")

        if not agent_state.restart_requested and not setup_required:
            short_term.active_provider = brain.current_provider_name
            short_term.active_model = brain.current_model
            short_term.save_to_file(snapshot_path)
            logger.info(f"Short-term memory snapshot saved ({len(short_term.primary)} messages)")

        logger.info(
            f"Teardown complete (reason={reason}); _main returning restart={agent_state.restart_requested}"
        )
        logger.remove()  # flush + close file handler，避免 handler 跨 _main() 调用残留

    return agent_state.restart_requested


def _restart_process(restart_signal: str | None) -> None:
    """Hand restart control to the platform launcher."""
    if sys.platform == "win32":
        if restart_signal is None:
            raise RuntimeError("Windows worker restart signal is unavailable")
        Path(restart_signal).touch()
        os._exit(0)
    argv = [sys.executable, "-m", "coworker", *sys.argv[1:]]
    logger.info("Replacing process via os.execv...")
    os.execv(sys.executable, argv)


def run_sync(restart_signal: str | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--check", action="store_true", help="校验代码环境（配置加载+Provider注册），不启动服务"
    )
    parser.add_argument(
        "--backfill-tree",
        action="store_true",
        help="从原始日志全史一次性重建多尺度记忆树，写回快照后退出",
    )
    args, _ = parser.parse_known_args()

    if args.check:
        sys.exit(asyncio.run(_run_check()))

    if args.backfill_tree:
        sys.exit(asyncio.run(_run_backfill()))

    restart = asyncio.run(_main())
    if restart:
        _restart_process(restart_signal)
