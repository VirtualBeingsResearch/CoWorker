"""复刻 `coworker.__main__:_main()` 的对象装配顺序，供 branch_runner 在独立工作目录/
独立进程里重建一份可 step 的 AgentLoop。

与生产入口的差异（刻意的，不是遗漏）：
- 不启动 uvicorn/生产 HTTP API、不接 WeCom/Codex 通道——这些是生产环境专属外壳。
- 不调用 `inbox_watcher.start()`（不做文件轮询式投递）；探索平台走 `POST /input`
  直接 `inbox_watcher.push(...)`，不需要监视 workdir 下的 inbox 目录。
- `agent.idle_sleep_seconds` 强制置 0：分支的节奏完全由 step/resume 主动驱动。
- communicate/list_connections 在 Explore Lab 中走模拟通信信道：schema 与调用行为
  保持可测，但不会真的向外部连接发消息。
- 不做生产那套「重启悬空 tool_call 修复 + 系统重启消息」——导入的快照本来就是
  当前有效状态，不是「重启恢复」场景，只做基础的 `cleanup_incomplete_tool_calls()`。

改动 `__main__.py` 里的工具注册/对象图装配顺序时，记得同步这里。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from coworker.agent.bubble import BubbleStore
from coworker.agent.event_collector import RuntimeEventCollector
from coworker.agent.inbox_watcher import InboxWatcher
from coworker.agent.interaction_log import InteractionLogger
from coworker.agent.log_store import LogStore
from coworker.agent.loop import AgentLoop
from coworker.agent.subconscious import SubconsciousScheduler
from coworker.agent.subconscious_mode import SubconsciousModeLoader
from coworker.agent.usage_stats import UsageStatsCollector
from coworker.brain.brain import Brain
from coworker.brain.factory import build_provider
from coworker.channels.system import create_channel_system
from coworker.core.config import Config, LLMConfig
from coworker.core.exceptions import ModelNotSupportedError, ProviderNotFoundError
from coworker.core.model_config import apply_runtime_model_config_file
from coworker.core.types import AgentState
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
from coworker.tools.communicate_tool import ListConnectionTool
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
from coworker.tools.vision_tools import ViewImageTool, VisualAnalysisTool
from coworker.tools.web_tools import FetchURLTool, SearchWebTool

from explore_lab.lab_communicate import LabCommunicateTool


def _build_stm_kwargs(config: Config, log_store: LogStore) -> dict:
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


def _pick_api_key(llm_cfg: LLMConfig, provider: str) -> str:
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
        except ValueError:
            pass


def load_branch_config(workdir: Path) -> Config:
    """从 workdir 根的 config.json（导入/fork 时落地）加载配置；不存在则退回默认值
    （env/.env，主要用于单测直接在空目录里装配）。"""
    config_path = workdir / "config.json"
    if config_path.is_file():
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        config = Config.model_validate(raw)
    else:
        config = Config()
    apply_runtime_model_config_file(config.llm)
    # 分支节奏完全由 step/resume 主动驱动，不走生产的「空闲打盹」计时器。
    config.agent.idle_sleep_seconds = 0
    return config


@dataclass
class Runtime:
    """一个分支的完整对象图，供 branch_runner 的控制 API 直接操作。"""

    workdir: Path
    config: Config
    identity: Identity
    skill_loader: SkillLoader
    palace_loader: PalaceLoader
    mode_loader: SubconsciousModeLoader
    log_store: LogStore
    interaction_log: InteractionLogger
    event_collector: RuntimeEventCollector
    usage_stats: UsageStatsCollector
    long_term: LongTermMemory
    recent_activity: RecentActivityMemory | None
    short_term: ShortTermMemory
    brain: Brain
    agent_state: AgentState
    inbox_watcher: InboxWatcher
    base_registry: ToolRegistry
    prompt_builder: SystemPromptBuilder
    bubble_store: BubbleStore | None
    subconscious: SubconsciousScheduler | None
    agent_loop: AgentLoop
    task_store: TaskStore
    snapshot_path: Path
    thinking_path: Path
    browser_store: BrowserSessionStore
    communicate: LabCommunicateTool
    tool_intercepts: dict[str, str]
    clear_short_term_memory_tool: ClearShortTermMemoryTool
    stm_kwargs: dict


def create_subconscious_scheduler(runtime: Runtime) -> SubconsciousScheduler:
    if runtime.bubble_store is None:
        runtime.bubble_store = BubbleStore(max_concurrent=runtime.config.agent.bubble_max_concurrent)
    runtime.agent_loop._bubble_store = runtime.bubble_store
    return SubconsciousScheduler(
        cfg=runtime.config,
        bubble_store=runtime.bubble_store,
        brain=runtime.brain,
        tool_registry=runtime.base_registry,
        prompt_builder=runtime.prompt_builder,
        short_term=runtime.short_term,
        inbox=runtime.inbox_watcher,
        logs_dir=runtime.config.agent.logs_dir,
        interaction_log=runtime.interaction_log,
        usage_stats=runtime.usage_stats,
        state_path=Path(runtime.config.memory.db_path) / "subconscious_state.json",
        task_store=runtime.task_store,
        palace_loader=runtime.palace_loader,
        long_term=runtime.long_term,
        mode_loader=runtime.mode_loader,
    )


def _apply_intercepts(runtime: Runtime) -> None:
    """把 runtime.tool_intercepts 应用到 base_registry 上（覆盖式，不是增量合并）。

    `ToolRegistry._intercepts` 是"私有"属性，但同一进程内直接赋值是最省心的做法：
    `intercept()` 只会合并/新增名单，没有"取消拦截"的公开 API；直接整体替换这个
    dict 才能支持"运行中把某个工具重新放行"。
    """
    runtime.base_registry._intercepts = dict(runtime.tool_intercepts)


async def assemble_runtime(workdir: Path) -> Runtime:
    workdir.mkdir(parents=True, exist_ok=True)
    config = load_branch_config(workdir)

    log_store = LogStore(config.agent.logs_dir)
    interaction_log = InteractionLogger(
        f"{config.agent.logs_dir}/interactions.jsonl",
        rotation_bytes=config.agent.interaction_log_rotation_bytes,
    )

    long_term = LongTermMemory(
        db_path=config.memory.db_path,
        llm_provider=config.memory.mem0_llm_provider,
        llm_api_key=_pick_api_key(config.llm, config.memory.mem0_llm_provider),
        llm_model=config.memory.mem0_llm_model,
        embedder_model=config.memory.mem0_embedder_model,
    )
    await long_term.initialize()

    recent_activity: RecentActivityMemory | None = None
    if config.memory.recent_activity_enabled:
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
    stm_kwargs = _build_stm_kwargs(config, log_store)
    if snapshot_path.exists() and _validate_snapshot(snapshot_path):
        short_term = ShortTermMemory.load_from_file(snapshot_path, **stm_kwargs)
        short_term.cleanup_incomplete_tool_calls()
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
    )
    _register_providers(brain, config)
    await brain.update_model_config(
        summary_provider=config.llm.summary_provider,
        summary_model=config.llm.summary_model,
        summary_thinking=config.llm.summary_thinking,
        fallbacks=config.llm.fallbacks,
        vision_provider=config.llm.vision_provider,
        vision_model=config.llm.vision_model,
    )

    if short_term.active_provider and short_term.active_model:
        try:
            await brain.switch_model(short_term.active_provider, short_term.active_model)
        except (ProviderNotFoundError, ModelNotSupportedError):
            pass

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
    )

    event_collector = RuntimeEventCollector(log_store, redact=agent_state._replace_ids)
    usage_stats = UsageStatsCollector(
        log_store,
        state_path=Path(config.agent.logs_dir) / "usage_stats.json",
    )
    usage_stats.load_bubble_history(config.agent.logs_dir)
    interaction_log.add_listener(event_collector.on_entry)
    interaction_log.add_listener(usage_stats.on_entry)

    inbox_watcher = InboxWatcher(config.agent.inbox_dir, config.agent.inbox_poll_interval)

    job_store = BackgroundJobStore()
    browser_store = BrowserSessionStore()
    registry = ToolRegistry()
    task_store = TaskStore("data/tasks.json")
    thinking_path = Path("data/thinking.md")

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
    registry.register(BrowserViewTool(
        browser_store, max_dimension=config.agent.image_max_dimension,
    ))
    registry.register(ExecuteCodeTool(
        store=job_store, hard_timeout=config.agent.code_hard_timeout, inbox=inbox_watcher,
    ))
    registry.register(GetCodeResultTool(job_store, inbox=inbox_watcher))
    registry.register(KillCodeJobTool(job_store))
    registry.register(QueryMemoryTool(
        long_term, short_term, brain, recent_activity=recent_activity,
    ))
    registry.register(ManageMemoryTool(long_term))
    registry.register(SleepTool(inbox_watcher))
    registry.register(BreatheTool())
    registry.register(SwitchModelTool(brain))

    alarm_manager = AlarmManager(
        inbox_watcher, persist_path=Path(config.memory.db_path) / "alarms.json",
    )
    await alarm_manager.restore()
    registry.register(SetAlarmTool(alarm_manager))
    registry.register(ListAlarmsTool(alarm_manager))
    registry.register(CancelAlarmTool(alarm_manager))

    channel_system = create_channel_system(config.agent.outbox_dir)
    channel_system.registry.set_inbound_handler(inbox_watcher.push)
    communicate = LabCommunicateTool(channel_system.registry)
    registry.register(communicate)
    registry.register(ListConnectionTool(communicate))
    registry.register(GetSkillTool(skill_loader, agent_state))
    registry.register(GetContextTool(brain, short_term, agent_state))
    registry.register(ManagePinnedContextTool(short_term))
    registry.register(RestartSelfTool(short_term=short_term, snapshot_path=snapshot_path))

    registry.register(VisualAnalysisTool(
        brain, inbox=inbox_watcher, max_dimension=config.agent.image_max_dimension,
    ))
    registry.register(ViewImageTool(max_dimension=config.agent.image_max_dimension))

    prompt_builder = SystemPromptBuilder(
        identity, registry, skill_loader,
        palace_loader=palace_loader,
        thinking_path=str(thinking_path),
        git_commit=None,
    )

    bubble_store: BubbleStore | None = None
    if config.agent.bubble_thinking:
        bubble_store = BubbleStore(max_concurrent=config.agent.bubble_max_concurrent)
        registry.register(BubbleSpawnTool(
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
            stream_runtime=channel_system.stream_runtime,
        ))
        registry.register(BubbleCheckTool(bubble_store))
        registry.register(BubbleSendTool(bubble_store, inbox_watcher))
        registry.register(BubbleCancelTool(bubble_store))
        registry.register(BubbleListTool(bubble_store))
        registry.register(BubbleDoneTool())

    clear_short_term_memory_tool = ClearShortTermMemoryTool(short_term, brain, None)
    registry.register(clear_short_term_memory_tool)

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
        subconscious=None,
        recent_activity=recent_activity,
    )

    runtime = Runtime(
        workdir=workdir,
        config=config,
        identity=identity,
        skill_loader=skill_loader,
        palace_loader=palace_loader,
        mode_loader=mode_loader,
        log_store=log_store,
        interaction_log=interaction_log,
        event_collector=event_collector,
        usage_stats=usage_stats,
        long_term=long_term,
        recent_activity=recent_activity,
        short_term=short_term,
        brain=brain,
        agent_state=agent_state,
        inbox_watcher=inbox_watcher,
        base_registry=registry,
        prompt_builder=prompt_builder,
        bubble_store=bubble_store,
        subconscious=None,
        agent_loop=agent_loop,
        task_store=task_store,
        snapshot_path=snapshot_path,
        thinking_path=thinking_path,
        browser_store=browser_store,
        communicate=communicate,
        tool_intercepts={},
        clear_short_term_memory_tool=clear_short_term_memory_tool,
        stm_kwargs=stm_kwargs,
    )
    if config.agent.subconscious_thinking:
        runtime.subconscious = create_subconscious_scheduler(runtime)
        agent_loop._subconscious = runtime.subconscious
        clear_short_term_memory_tool._subconscious = runtime.subconscious
    _apply_intercepts(runtime)
    return runtime


def set_tool_intercepts(runtime: Runtime, intercepts: dict[str, str]) -> None:
    """整体替换（不是合并）该分支的工具拦截名单，下一次 `_cycle()` 起生效。"""
    runtime.tool_intercepts = dict(intercepts)
    _apply_intercepts(runtime)
