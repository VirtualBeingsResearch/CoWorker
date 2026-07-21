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
from coworker.api.admin import setup_admin
from coworker.api.routes import setup as setup_routes
from coworker.brain.brain import Brain
from coworker.brain.factory import build_provider
from coworker.channels.desktop import (
    DESKTOP_PREFIX,
    DesktopCommunicateSender,
    DesktopDispatcher,
    DesktopRegistry,
)
from coworker.channels.wecom import WeComRunner
from coworker.core.config import Config, LLMConfig, apply_admin_config_file, ensure_admin_token
from coworker.core.diagnostics import format_task_stacks, task_snapshot
from coworker.core.exceptions import ModelNotSupportedError, ProviderNotFoundError
from coworker.core.logging import intercept_standard_logging
from coworker.core.model_config import apply_runtime_model_config_file
from coworker.core.types import AgentState, IncomingEvent, Message
from coworker.identity.identity import Identity
from coworker.memory.long_term import LongTermMemory
from coworker.memory.recent_activity import RecentActivityMemory
from coworker.memory.short_term import ShortTermMemory
from coworker.palaces.loader import PalaceLoader
from coworker.prompts.system_prompt import SystemPromptBuilder
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
from coworker.tools.communicate_tool import CommunicateTool, ListWSConnectionsTool
from coworker.tools.file_tools import (
    FindFilesTool,
    GrepFilesTool,
    ListDirectoryTool,
    ReadFileTool,
    WriteFileTool,
)
from coworker.tools.memory_tools import ManageMemoryTool, QueryMemoryTool
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
    logger.add(sys.stderr, level="INFO", colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}")
    logger.add(f"{logs_dir}/coworker.log", rotation="10 MB", retention="7 days",
               level="DEBUG", encoding="utf-8")
    intercept_standard_logging()


def _get_env_snapshot() -> dict:
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
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            snapshot["git_commit"] = r.stdout.strip()
    except Exception:
        pass
    return snapshot


def _diff_env(old: dict, new: dict) -> str | None:
    _LABELS = {
        "python_version": "Python 版本",
        "python_executable": "Python 解释器",
        "os": "操作系统",
        "machine": "架构",
        "cwd": "工作目录",
        "git_commit": "代码版本",
    }
    changes = []
    for key, label in _LABELS.items():
        ov, nv = old.get(key), new.get(key)
        if ov is not None and nv is not None and ov != nv:
            changes.append(f"{label}：{ov} → {nv}")
    return ("环境变化：" + "；".join(changes)) if changes else None


def _find_pending_tool_call(messages: list, tool_name: str) -> dict | None:
    """检查 primary 末尾是否有未完成的指定 tool call，返回 {id} 或 None。"""
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.role == "assistant" and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.get("function", {}).get("name") == tool_name:
                    tc_id = tc.get("id", "")
                    has_result = any(
                        m.role == "tool" and m.tool_call_id == tc_id
                        for m in messages[i + 1:]
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

    short_term.primary.append(Message(
        role="tool",
        content=content,
        tool_call_id=pending["id"],
    ))
    interaction_log.log_tool_result(pending["id"], tool_name, content, is_error=False)
    return True


def _pick_api_key(llm_cfg: LLMConfig, provider: str) -> str:
    """按实例名解析 api_key（mem0 用）。先查解析后的命名 provider，再回退到名即类型。"""
    for spec in llm_cfg.resolved_providers():
        if spec.name == provider:
            return spec.api_key
    return ""


def _register_providers(brain: Brain, config: Config) -> None:
    for spec in config.llm.resolved_providers():
        try:
            brain.register_provider(build_provider(
                spec.type,
                spec.api_key,
                base_url=spec.base_url or None,
                name=spec.name,
                default_model=spec.default_model,
            ))
        except ValueError as e:
            logger.error(f"跳过 provider {spec.name!r}：{e}")


def _load_config() -> Config:
    config = apply_admin_config_file(Config())
    apply_runtime_model_config_file(config.llm)
    return config


async def _validate_model_runtime_config(brain: Brain, config: Config) -> None:
    await brain.update_model_config(
        summary_provider=config.llm.summary_provider,
        summary_model=config.llm.summary_model,
        summary_thinking=config.llm.summary_thinking,
        fallbacks=config.llm.fallbacks,
        vision_provider=config.llm.vision_provider,
        vision_model=config.llm.vision_model,
        vision_thinking=config.llm.vision_thinking,
    )


async def _run_check() -> int:
    """--check 模式：走配置加载 + Provider 注册，不启动服务。0=通过，1=失败。"""
    try:
        config = _load_config()
        _setup_logging(config.agent.logs_dir)
        brain = Brain(
            config.llm.default_provider,
            config.llm.default_model,
            message_time_prefix=config.agent.message_time_prefix,
            max_tokens=config.llm.max_tokens,
            fallbacks=config.llm.fallbacks,
            summary_provider=config.llm.summary_provider,
            summary_model=config.llm.summary_model,
            summary_thinking=config.llm.summary_thinking,
            vision_provider=config.llm.vision_provider,
            vision_model=config.llm.vision_model,
            vision_thinking=config.llm.vision_thinking,
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
        compress_threshold=config.memory.compress_threshold,
        compress_ratio=config.memory.compress_ratio,
        compress_protected_tail=config.memory.compress_protected_tail,
        log_store=log_store,
        tree_enabled=config.memory.tree_enabled,
        tree_tail_fraction=config.memory.tree_tail_fraction,
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
            summary_provider=config.llm.summary_provider,
            summary_model=config.llm.summary_model,
            summary_thinking=config.llm.summary_thinking,
            vision_provider=config.llm.vision_provider,
            vision_model=config.llm.vision_model,
            vision_thinking=config.llm.vision_thinking,
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
            )
        )
        if short_term.active_provider and short_term.active_model:
            try:
                await brain.switch_model(short_term.active_provider, short_term.active_model)
            except (ProviderNotFoundError, ModelNotSupportedError) as e:
                logger.warning(f"[backfill] Could not restore previous model: {e}")

        logger.info("[backfill] 开始从原始日志全史回溯记忆树……")
        n = await short_term.backfill_tree_from_log(
            brain, max_leaves=config.memory.tree_backfill_max_leaves
        )
        short_term.save_to_file(snapshot_path)
        logger.info(
            f"[backfill] 完成：生成 {n} 叶子，脊柱 {len(short_term.tree.nodes)} 节点，已写回快照。"
        )
        return 0
    except Exception as e:
        logger.error(f"[backfill] FAIL: {e}")
        return 1


async def _main() -> bool:
    """主入口。返回 True 表示请求重启，由 main_sync() 调用 _exec_replace() 处理。"""
    config = _load_config()
    _setup_logging(config.agent.logs_dir)
    generated_admin_token = ensure_admin_token(config)
    if generated_admin_token:
        # Deliberately bypass loguru: the one-time credential must not be copied
        # into the persistent coworker.log file.
        print(
            "\n" + "=" * 68
            + "\nCoworker 首次启动：请用下面的初始管理员令牌打开 /admin\n\n"
            + f"  {generated_admin_token}\n\n"
            + f"令牌已保存到 {config.admin.config_file}，完成初始化后仍可继续使用。\n"
            + "=" * 68 + "\n",
            file=sys.stderr,
            flush=True,
        )
    logger.info("Starting coworker")
    first_run_setup = not any(
        spec.name == config.llm.default_provider and bool(spec.api_key)
        for spec in config.llm.resolved_providers()
    )
    interaction_log = InteractionLogger(
        f"{config.agent.logs_dir}/interactions.jsonl",
        rotation_bytes=config.agent.interaction_log_rotation_bytes,
    )
    # 原始日志的只读寻址层，供记忆块树按时间区间重摘要 / 下钻；抗后续分片轮转。
    log_store = LogStore(config.agent.logs_dir)

    long_term = LongTermMemory(
        db_path=config.memory.db_path,
        llm_provider=config.memory.mem0_llm_provider,
        llm_api_key=_pick_api_key(config.llm, config.memory.mem0_llm_provider),
        llm_model=config.memory.mem0_llm_model,
        embedder_model=config.memory.mem0_embedder_model,
    )
    if first_run_setup:
        Path(config.memory.db_path).mkdir(parents=True, exist_ok=True)
        logger.warning("Long-term memory initialization deferred until first-run setup completes")
    else:
        await long_term.initialize()

    recent_activity: RecentActivityMemory | None = None
    if config.memory.recent_activity_enabled and not first_run_setup:
        recent_activity = RecentActivityMemory(
            db_path=config.memory.db_path,
            log_store=log_store,
            embedder_model=config.memory.mem0_embedder_model,
            days=config.memory.recent_activity_days,
            chunk_tokens=config.memory.recent_activity_chunk_tokens,
            overlap_tokens=config.memory.recent_activity_overlap_tokens,
            chroma_client=long_term.chroma_client,
            embedder=long_term.embedder,
        )

    snapshot_path = Path(config.memory.db_path) / "short_term_snapshot.json"
    alarm_persist_path = Path(config.memory.db_path) / "alarms.json"
    env_snapshot_path = Path(config.memory.db_path) / "env_snapshot.json"

    current_env = _get_env_snapshot()
    prev_env: dict = {}
    if env_snapshot_path.exists():
        try:
            prev_env = json.loads(env_snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    env_diff = _diff_env(prev_env, current_env) if prev_env else None
    env_snapshot_path.write_text(json.dumps(current_env, ensure_ascii=False, indent=2), encoding="utf-8")
    if env_diff:
        logger.info(f"Environment changed since last run: {env_diff}")

    # 快照自检：损坏则删除，降级为全新启动
    snapshot_valid = snapshot_path.exists() and _validate_snapshot(snapshot_path)
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
            content = f"重启成功。当前时间：{now_str}。已恢复 {len(short_term.primary)} 条消息"
            if alarm_count:
                content += f"，{alarm_count} 个闹钟"
            content += "。"
            if env_diff:
                content += f" {env_diff}。"
            _append_recovered_tool_result(
                short_term,
                interaction_log,
                tool_name="restart_self",
                content=content,
            )

        sleep_content = f"睡眠被系统重启中断。当前时间：{now_str}。"
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

    if recent_activity is not None:
        recent_activity.start_background_initialization(short_term.raw_primary_boundary())

    brain = Brain(
        config.llm.default_provider,
        config.llm.default_model,
        message_time_prefix=config.agent.message_time_prefix,
        max_tokens=config.llm.max_tokens,
        fallbacks=config.llm.fallbacks,
        summary_provider=config.llm.summary_provider,
        summary_model=config.llm.summary_model,
        summary_thinking=config.llm.summary_thinking,
        vision_provider=config.llm.vision_provider,
        vision_model=config.llm.vision_model,
        vision_thinking=config.llm.vision_thinking,
    )
    _register_providers(brain, config)
    await _validate_model_runtime_config(brain, config)

    if brain.active_provider is None:
        # A fresh installation should expose the API and setup UI without
        # autonomously attempting calls against an unconfigured provider.
        config.agent.tick = False
        logger.warning("No active LLM provider; running in first-run setup mode")

    if short_term.active_provider and short_term.active_model:
        try:
            await brain.switch_model(short_term.active_provider, short_term.active_model)
            logger.info(f"Restored model from snapshot: {short_term.active_provider}/{short_term.active_model}")
        except (ProviderNotFoundError, ModelNotSupportedError) as e:
            logger.warning(f"Could not restore previous model ({short_term.active_provider}/{short_term.active_model}): {e}")

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
        setup_mode=first_run_setup,
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
    def log_mem0_usage(entry: dict[str, Any]) -> None:
        usage = entry.get("usage")
        interaction_log.log_mem0_llm_response(
            provider=str(entry.get("provider") or "unknown"),
            model=str(entry.get("model") or "unknown"),
            usage=usage if isinstance(usage, dict) else {},
            usage_source=str(entry.get("usage_source") or ""),
            operation=str(entry.get("operation") or ""),
        )

    long_term.add_usage_listener(log_mem0_usage)
    brain.add_summary_usage_listener(
        lambda response, meta: interaction_log.log_summary_llm_response(
            provider=response.provider,
            model=response.model,
            usage=response.usage,
            context_hint=str(meta.get("context_hint") or ""),
        )
    )
    brain.add_vision_usage_listener(
        lambda response, meta: interaction_log.log_vision_llm_response(
            provider=response.provider,
            model=response.model,
            usage=response.usage,
            label=str(meta.get("label") or ""),
        )
    )

    inbox_watcher = InboxWatcher(config.agent.inbox_dir, config.agent.inbox_poll_interval)

    communicate = CommunicateTool(config.agent.outbox_dir)
    job_store = BackgroundJobStore()
    browser_store = BrowserSessionStore()
    registry = ToolRegistry()
    task_store = TaskStore("data/tasks.json")
    desktop_registry = DesktopRegistry(short_term, config.agent.desktop_registry_dir)
    short_term.unpin("codex_registry")

    desktop_dispatcher = DesktopDispatcher(desktop_registry)
    inbox_watcher.set_interceptor(desktop_dispatcher)
    communicate.add_connection_listener(
        lambda: desktop_registry.update_connections(set(communicate.list_connected()))
    )
    desktop_registry.update_connections(set(communicate.list_connected()))
    registry.register(TaskCreateTool(task_store))
    registry.register(TaskGetTool(task_store))
    registry.register(TaskListTool(task_store))
    registry.register(TaskUpdateTool(task_store))
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(ListDirectoryTool())
    registry.register(FindFilesTool())
    registry.register(GrepFilesTool())
    registry.register(SearchWebTool())
    registry.register(FetchURLTool())
    registry.register(BrowserOpenTool(browser_store))
    registry.register(BrowserScreenshotTool(browser_store))
    registry.register(BrowserActionTool(browser_store))
    registry.register(BrowserGetContentTool(browser_store))
    registry.register(BrowserCloseTool(browser_store))
    registry.register(BrowserListSessionsTool(browser_store))
    registry.register(BrowserViewTool(browser_store, max_dimension=config.agent.image_max_dimension))
    registry.register(ExecuteCodeTool(store=job_store, hard_timeout=config.agent.code_hard_timeout, inbox=inbox_watcher))
    registry.register(GetCodeResultTool(job_store, inbox=inbox_watcher))
    registry.register(KillCodeJobTool(job_store))
    registry.register(QueryMemoryTool(
        long_term, short_term, brain, recent_activity=recent_activity,
    ))
    registry.register(ManageMemoryTool(long_term))
    registry.register(SleepTool(inbox_watcher, config=config))
    registry.register(BreatheTool())
    registry.register(SwitchModelTool(brain))
    alarm_manager = AlarmManager(inbox_watcher, persist_path=alarm_persist_path)
    restored_alarms = await alarm_manager.restore()
    registry.register(SetAlarmTool(alarm_manager))
    registry.register(ListAlarmsTool(alarm_manager))
    registry.register(CancelAlarmTool(alarm_manager))
    registry.register(communicate)
    registry.register(ListWSConnectionsTool(communicate))
    registry.register(GetSkillTool(skill_loader, agent_state))
    registry.register(GetContextTool(brain, short_term, agent_state))
    registry.register(ManagePinnedContextTool(short_term))
    registry.register(RestartSelfTool(short_term=short_term, snapshot_path=snapshot_path))

    from coworker.tools.vision_tools import ViewImageTool, VisualAnalysisTool
    registry.register(VisualAnalysisTool(brain, inbox=inbox_watcher, max_dimension=config.agent.image_max_dimension))
    registry.register(ViewImageTool(max_dimension=config.agent.image_max_dimension))

    prompt_builder = SystemPromptBuilder(identity, registry, skill_loader, palace_loader=palace_loader, thinking_path="data/thinking.md", git_commit=current_env.get("git_commit"))

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
            handoff_matcher=BubbleHandoffMatcher.from_config(
                participant_matches=(config.agent.bubble_handoff_transparency_participant_matches),
                stream_transports=(config.agent.bubble_handoff_transparency_stream_transports),
            ),
        )
        registry.register(bubble_spawn)
        registry.register(BubbleCheckTool(bubble_store))
        registry.register(BubbleSendTool(bubble_store, inbox_watcher))
        registry.register(BubbleCancelTool(bubble_store))
        registry.register(BubbleListTool(bubble_store))
        registry.register(BubbleDoneTool())

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

    if is_restart:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        restart_msg = f"系统已重启。当前时间：{now_str}。"
        if restored_alarms:
            restart_msg += f"已恢复 {restored_alarms} 个待触发闹钟。"
        if env_diff:
            restart_msg += f" {env_diff}。"
        await inbox_watcher.push(IncomingEvent(
            participant_id="system",
            content=restart_msg,
            source="system",
        ))
    elif env_diff:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        await inbox_watcher.push(IncomingEvent(
            participant_id="system",
            content=f"系统启动。当前时间：{now_str}。{env_diff}。",
            source="system",
        ))

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
        recent_activity=recent_activity,
    )

    setup_routes(
        inbox_watcher,
        agent_loop,
        brain,
        config.agent.inbox_dir,
        usage_stats,
        config.llm.runtime_config_file,
        config.api.communication_token,
        config.api.development_mode,
    )
    setup_admin(
        agent=agent_loop,
        brain=brain,
        config=config,
        alarm_manager=alarm_manager,
        skill_loader=skill_loader,
        palace_loader=palace_loader,
        mode_loader=mode_loader,
    )
    api_app.setup_desktop_updates(config.desktop_updates, config.admin.token)
    api_app.setup_ws(inbox_watcher, communicate)
    api_app.set_collector(event_collector)

    desktop_sender = DesktopCommunicateSender(communicate)
    communicate.register_sender(DESKTOP_PREFIX, desktop_sender.send, supports_extra=True)

    wecom_runner: WeComRunner | None = None
    if config.wecom.enabled:
        if not config.wecom.bot_id or not config.wecom.secret:
            logger.warning("WeCom enabled but bot_id/secret missing; skipping")
        else:
            wecom_runner = WeComRunner(
                cfg=config.wecom,
                inbox=inbox_watcher,
                attachments_dir=Path(config.agent.inbox_dir).parent / "attachments",
                contacts_path=Path(config.memory.db_path) / "wecom_contacts.json",
            )
            communicate.register_sender("wecom:", wecom_runner.sender, wecom_runner.checker)
            logger.info(f"WeCom runner prepared, bot_id={config.wecom.bot_id}")

    # 写入实例状态文件（新旧交接标记）
    status_path = Path(config.memory.db_path) / "instance_status.json"
    status_path.write_text(json.dumps({
        "pid": os.getpid(),
        "started_at": datetime.now().isoformat(),
        "is_restart": is_restart,
        "messages_restored": len(short_term.primary),
        "alarms_restored": restored_alarms,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
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

    inbox_task = asyncio.create_task(inbox_watcher.start(), name="inbox")
    server_task = asyncio.create_task(server.serve(), name="server")
    loop_task = asyncio.create_task(agent_loop.run(), name="loop")
    wecom_task: asyncio.Task | None = None
    if wecom_runner is not None:
        wecom_task = asyncio.create_task(wecom_runner.start(), name="wecom")

    try:
        # 等待 agent_loop 退出（正常关闭或重启请求），然后关闭其他服务
        await loop_task
    finally:
        reason = "restart" if agent_state.restart_requested else "shutdown"
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
        # 到不了 _exec_replace。
        #
        # 对策：主动唤醒 communicate/pool 的 /sse、/ws 出站队列，让它们立即跳出循环、释放连接
        # → 连接数归零 → wait_closed() 立刻返回。连接清零后无需 force_exit，uvicorn 能正常走
        # lifespan.shutdown（force_exit 会跳过它，反而导致 lifespan 任务被取消、刷 CancelledError
        # 噪声）。timeout_graceful_shutdown=3 仅作兜底，正常路径用不到。
        api_app.signal_shutdown()
        server.should_exit = True
        inbox_watcher.stop()
        if wecom_runner is not None:
            await wecom_runner.stop()
            logger.info("WeCom runner stopped")
        background = [inbox_task, server_task]
        if wecom_task is not None:
            background.append(wecom_task)
        # 用 asyncio.wait（不取消 pending，便于如实记录谁卡住），超时后再点名 + 打栈 + 取消。
        _, pending = await asyncio.wait(background, timeout=10)
        if pending:
            logger.warning(
                "后台任务 10s 内未停止，强制取消以继续退出/重启；卡住的 task: "
                + ", ".join(t.get_name() for t in pending)
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

        if not agent_state.restart_requested:
            short_term.active_provider = brain.current_provider_name
            short_term.active_model = brain.current_model
            short_term.save_to_file(snapshot_path)
            logger.info(f"Short-term memory snapshot saved ({len(short_term.primary)} messages)")

        logger.info(f"Teardown complete (reason={reason}); _main returning restart={agent_state.restart_requested}")
        logger.remove()  # flush + close file handler，避免 handler 跨 _main() 调用残留

    return agent_state.restart_requested


def _exec_replace() -> None:
    """跨平台进程替换。

    Unix: os.execv 原地替换（同 PID，继承所有 FD）。
    Windows: 模拟 cargo-util exec_replace —— 父进程忽略 Ctrl-C（Windows 会把 Ctrl-C 广播给
    同一 console group 的所有进程，子进程自然收到），spawn 子进程并阻塞等待，最终以子进程退出码退出。
    终端始终保持连接，子进程行为对用户透明。
    """
    argv = [sys.executable, "-m", "coworker"] + sys.argv[1:]
    if sys.platform == "win32":
        import ctypes
        import subprocess
        # SetConsoleCtrlHandler(NULL, TRUE): 父进程忽略 Ctrl-C，
        # 子进程作为同一 console group 成员会直接收到信号。
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, True)
        proc = subprocess.Popen(argv)
        proc.wait()
        sys.exit(proc.returncode)
    else:
        logger.info("Replacing process via os.execv...")
        os.execv(sys.executable, argv)


def main_sync() -> None:
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--check", action="store_true",
                        help="校验代码环境（配置加载+Provider注册），不启动服务")
    parser.add_argument("--backfill-tree", action="store_true",
                        help="从原始日志全史一次性重建多尺度记忆树，写回快照后退出")
    args, _ = parser.parse_known_args()

    if args.check:
        sys.exit(asyncio.run(_run_check()))

    if args.backfill_tree:
        sys.exit(asyncio.run(_run_backfill()))

    restart = asyncio.run(_main())
    if restart:
        _exec_replace()


if __name__ == "__main__":
    main_sync()
