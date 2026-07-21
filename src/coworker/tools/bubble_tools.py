from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from coworker.agent.bubble_handoff import (
    BubbleHandoffMatcher,
)
from coworker.core.types import ToolResult
from coworker.i18n import bind_locale, tr
from coworker.tools.base import Tool, ToolDefinition

if TYPE_CHECKING:
    from coworker.agent.bubble import Bubble, BubbleStore
    from coworker.agent.inbox_watcher import InboxWatcher
    from coworker.agent.interaction_log import InteractionLogger
    from coworker.agent.usage_stats import UsageStatsCollector
    from coworker.brain.brain import Brain
    from coworker.core.types import Message
    from coworker.memory.long_term import LongTermMemory
    from coworker.memory.short_term import ShortTermMemory
    from coworker.palaces.loader import Palace, PalaceLoader
    from coworker.prompts.system_prompt import SystemPromptBuilder
    from coworker.skills.loader import SkillLoader
    from coworker.tools.communicate_tool import CommunicateTool
    from coworker.tools.registry import ToolRegistry


def _create_bubble_brain(
    parent_brain: Brain,
    provider: str,
    model: str,
    thinking: bool = True,
) -> Brain:
    from coworker.brain.brain import Brain as _Brain

    bubble_brain = _Brain(
        default_provider=provider,
        default_model=model,
        message_time_prefix=parent_brain.message_time_prefix,
        max_tokens=parent_brain.max_tokens,
        fallbacks=parent_brain._fallbacks,
        thinking=thinking,
        summary_provider=parent_brain.summary_provider_name,
        summary_model=parent_brain.summary_model,
        summary_thinking=parent_brain.summary_thinking,
        vision_provider=parent_brain.vision_provider_name,
        vision_model=parent_brain.vision_model,
        vision_thinking=parent_brain.vision_thinking,
    )
    for provider_obj in parent_brain._providers.values():
        bubble_brain.register_provider(provider_obj)
    return bubble_brain


def _resolve_bubble_model_config(
    parent_brain: Brain,
    provider: str,
    model: str,
) -> tuple[str, str]:
    resolved_provider = provider or parent_brain.current_provider_name
    if model:
        return resolved_provider, model
    if resolved_provider == parent_brain.current_provider_name:
        return resolved_provider, parent_brain.current_model

    provider_obj = parent_brain._providers.get(resolved_provider)
    if provider_obj and provider_obj.default_model:
        return resolved_provider, provider_obj.default_model
    return resolved_provider, ""


class BubbleSpawnTool(Tool):
    def __init__(
        self,
        store: BubbleStore,
        short_term: ShortTermMemory,
        parent_brain: Brain,
        full_registry: ToolRegistry,
        system_prompt_builder: SystemPromptBuilder,
        inbox: InboxWatcher,
        logs_dir: str = "data/logs",
        parent_log: InteractionLogger | None = None,
        usage_stats: UsageStatsCollector | None = None,
        palace_loader: PalaceLoader | None = None,
        skill_loader: SkillLoader | None = None,
        long_term: LongTermMemory | None = None,
        communicate: CommunicateTool | None = None,
        handoff_matcher: BubbleHandoffMatcher | None = None,
    ) -> None:
        self._store = store
        self._short_term = short_term
        self._parent_brain = parent_brain
        self._full_registry = full_registry
        self._prompt_builder = system_prompt_builder
        self._inbox = inbox
        self._logs_dir = logs_dir
        self._parent_log = parent_log
        self._usage_stats = usage_stats
        self._palace_loader = palace_loader
        self._skill_loader = skill_loader
        self._long_term = long_term
        self._communicate = communicate
        self._handoff_matcher = handoff_matcher or BubbleHandoffMatcher()

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="bubble_spawn",
            description=(
                "创建一个独立的「思考泡泡」并发执行子任务，有独立的 LLM 实例，可并行运行。\n"
                "若传入 bubble_id，则不新建泡泡，而是在配置宽限期内从原上下文续跑已超时的泡泡；"
                "此时 goal 可作为可选的续跑指令，max_cycles 表示额外增加的轮次。\n"
                "默认继承当前短期记忆快照（forked）；设置 fresh_start=true 则全新开始，"
                "仅保留 pinned context，适合需要干净上下文的任务。\n"
                "palaces 可挂上一个或多个「记忆宫殿」，把对应领域的速记卡、关键 skill、相关长期记忆注入泡泡，"
                "适合专项任务执行（与 fresh_start 正交，专项执行建议 fresh_start=true 取得干净的领域上下文）。\n"
                "完成后自动将结论合并到主上下文，并推送完成通知。\n"
                "使用 bubble_check 查看进度，bubble_send 与泡泡通信，bubble_cancel 取消；"
                "超时后以 bubble_spawn(bubble_id=...) 续跑。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "新建泡泡的具体目标；未传 bubble_id 时必填。传入 bubble_id 续跑时，作为可选的续跑指令。",
                    },
                    "bubble_id": {
                        "type": "string",
                        "description": "要续跑的 timeout 泡泡 ID；传入后不新建泡泡，goal 可作为续跑指令。",
                    },
                    "max_cycles": {
                        "type": "integer",
                        "description": "新建时的最大执行轮次；续跑时的额外轮次（均为 1-50，累计总轮次不超过 50），默认 10。",
                        "default": 10,
                    },
                    "fresh_start": {
                        "type": "boolean",
                        "description": "全新开始：不继承当前对话历史，仅保留 pinned context。适合需要独立干净上下文的任务，默认 false。",
                        "default": False,
                    },
                    "palaces": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "挂载的记忆宫殿名称列表（见 [PALACES]）。注入领域速记卡、关键 skill 和按标签召回的相关长期记忆。可挂多个，留空则不挂。",
                    },
                    "participant_id": {
                        "type": "string",
                        "description": "该任务服务的对象 id（如某用户）。填上后，该对象的后续通信会在无歧义时直接转交给此活跃泡泡；泡泡也只能直接回复该对象。非特定对象可留空。",
                    },
                    "conversation_id": {
                        "type": "string",
                        "description": "可选的会话 ID。与 participant_id 一同绑定时，匹配该会话的后续通信优先直接转交给此泡泡，并限制泡泡只在该会话内回复。",
                    },
                    "thinking": {
                        "type": "boolean",
                        "description": "是否启用扩展思考模式。默认 true（开启）；设为 false 可跳过思考直接执行，适合调工具、数据汇总等快速任务，响应更快、token 消耗更少。",
                        "default": True,
                    },
                    "provider": {
                        "type": "string",
                        "description": "泡泡创建时使用的模型 provider。留空则继承当前主线程 provider。",
                    },
                    "model": {
                        "type": "string",
                        "description": "泡泡创建时使用的模型。留空则继承当前主线程 model。",
                    },
                },
                "required": [],
            },
        )

    async def execute(
        self,
        goal: str = "",
        bubble_id: str = "",
        max_cycles: int = 10,
        fresh_start: bool = False,
        palaces: list[str] | None = None,
        participant_id: str = "",
        conversation_id: str = "",
        thinking: bool = True,
        provider: str = "",
        model: str = "",
        **_,
    ) -> ToolResult:
        from coworker.core.token_utils import estimate_content_tokens

        if bubble_id:
            return await self._resume_existing(
                bubble_id=bubble_id,
                additional_cycles=max_cycles,
                continuation=goal,
            )
        if not goal.strip():
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.bubble.needs_goal"),
                is_error=True,
            )

        resolved_participant_id = participant_id.strip() if isinstance(participant_id, str) else ""
        if resolved_participant_id and self._communicate is not None:
            try:
                resolved_participant_id = self._communicate.resolve_participant_id(
                    resolved_participant_id
                )
            except ValueError as error:
                return ToolResult(tool_call_id="", content=str(error), is_error=True)

        max_cycles = max(1, min(max_cycles, 50))
        provider, model = _resolve_bubble_model_config(self._parent_brain, provider, model)
        provider_obj = self._parent_brain._providers.get(provider)
        if provider_obj is None:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.bubble.unknown_provider", provider=provider),
                is_error=True,
            )
        if not model:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.bubble.provider_no_model", provider=provider),
                is_error=True,
            )
        if not provider_obj.supports_tool_use(model):
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.bubble.model_no_tools", model=model, provider=provider),
                is_error=True,
            )

        # 解析并校验挂载的宫殿（在创建泡泡前，未知名直接报错）
        resolved_palaces = []
        if palaces:
            if self._palace_loader is None:
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.bubble.palaces_disabled"),
                    is_error=True,
                )
            for pname in palaces:
                palace = self._palace_loader.get(pname)
                if palace is None:
                    available = ", ".join(self._palace_loader.list_names()) or tr(
                        "tool_result.bubble.none"
                    )
                    return ToolResult(
                        tool_call_id="",
                        content=tr(
                            "tool_result.bubble.palace_missing",
                            name=pname,
                            available=available,
                        ),
                        is_error=True,
                    )
                resolved_palaces.append(palace)

        if fresh_start:
            # Only carry pinned items — no conversation history
            forked_context = self._short_term.pinned_as_messages()
        else:
            forked_context = list(self._short_term.primary)

        parent_tree = self._short_term.tree
        if parent_tree.nodes:
            forked_tree = parent_tree.clone_empty()
            forked_tree.nodes = list(parent_tree.nodes)
        else:
            forked_tree = None

        result = self._store.create(
            goal=goal,
            forked_context=forked_context,
            max_cycles=max_cycles,
            provider=provider,
            model=model,
        )
        if isinstance(result, str):
            return ToolResult(tool_call_id="", content=result, is_error=True)
        bubble = result
        bubble.forked_tree = forked_tree
        bubble.participant_id = resolved_participant_id
        bubble.conversation_id = conversation_id.strip() if isinstance(conversation_id, str) else ""
        bubble.handoff_transparency = self._should_use_handoff_transparency(bubble.participant_id)
        bubble.palaces = [p.name for p in resolved_palaces]

        if not fresh_start:
            # The snapshot ends with the current assistant[tool_use] message, which has no
            # tool_results yet. Synthesize them so the bubble's context is structurally valid:
            # - bubble_spawn → filled with this bubble's own id
            # - other tool calls → note that they run in the main loop after the fork
            primary = self._short_term.primary
            if primary and primary[-1].role == "assistant" and primary[-1].tool_calls:
                from coworker.core.types import Message as _Msg

                for tc in primary[-1].tool_calls:
                    tc_name = tc.get("function", {}).get("name", "")
                    tc_id = tc.get("id", "")
                    if tc_name == "bubble_spawn" and goal in tc.get("function", {}).get(
                        "arguments", ""
                    ):
                        content = tr("tool_result.bubble.forked", id=bubble.id)
                    else:
                        content = tr("tool_result.bubble.tool_after_fork", name=tc_name)
                    bubble.forked_context.append(
                        _Msg(
                            role="tool",
                            content=content,
                            tool_call_id=tc_id,
                        )
                    )

        # 注入宫殿：关键 skill body + 领域速记卡 + 按标签召回的长期记忆。
        # 放在结构已完整的 forked_context 末尾，作为泡泡启动前最新、最显著的上下文
        # （fork 边界用 len(forked_context) 计算，这些消息不会被算进 inner_messages）。
        if resolved_palaces:
            await self._inject_palaces(bubble, resolved_palaces, goal)

        bubble_brain = _create_bubble_brain(
            self._parent_brain,
            provider=bubble.provider,
            model=bubble.model,
            thinking=thinking,
        )
        bubble.brain = bubble_brain
        forked_tokens = sum(estimate_content_tokens(m.content) for m in bubble.forked_context)
        self.start_existing(bubble)

        context_desc = tr(
            "tool_result.bubble.fresh_context"
            if fresh_start
            else "tool_result.bubble.forked_context",
            count=len(bubble.forked_context),
            **({} if fresh_start else {"tokens": forked_tokens}),
        )
        palace_desc = (
            tr(
                "tool_result.bubble.mounted_palaces",
                names=", ".join(p.name for p in resolved_palaces),
            )
            if resolved_palaces
            else ""
        )
        thinking_desc = "" if thinking else tr("tool_result.bubble.fast_mode")
        model_desc = tr("tool_result.bubble.model", provider=bubble.provider, model=bubble.model)
        communication_desc = ""
        if bubble.participant_id:
            conversation = (
                tr(
                    "tool_result.bubble.conversation",
                    conversation=bubble.conversation_id,
                )
                if bubble.conversation_id
                else ""
            )
            communication_desc = tr(
                "tool_result.bubble.binding",
                participant=bubble.participant_id,
                conversation=conversation,
            )
            if bubble.handoff_transparency:
                communication_desc += tr("tool_result.bubble.transparent")
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.bubble.created",
                id=bubble.id,
                goal=goal,
                cycles=max_cycles,
                context=context_desc,
                palaces=palace_desc,
                thinking=thinking_desc,
                model=model_desc,
                communication=communication_desc,
            ),
        )

    async def _resume_existing(
        self,
        bubble_id: str,
        additional_cycles: int,
        continuation: str,
    ) -> ToolResult:
        """Resume a recently timed-out bubble without losing its forked context."""
        from coworker.agent.bubble_loop import BubbleMiniLoop

        try:
            requested_cycles = int(additional_cycles)
        except (TypeError, ValueError):
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.bubble.max_cycles_integer"),
                is_error=True,
            )
        requested_cycles = max(1, min(requested_cycles, BubbleMiniLoop._CYCLES_HARD_CAP))

        previous = self._store.get(bubble_id)
        previous_max_cycles = previous.max_cycles if previous is not None else 0
        result = self._store.resume(
            bubble_id,
            additional_cycles=requested_cycles,
            max_cycles_cap=BubbleMiniLoop._CYCLES_HARD_CAP,
        )
        if isinstance(result, str):
            return ToolResult(tool_call_id="", content=result, is_error=True)
        bubble = result

        if continuation.strip():
            await bubble.inbox.put((tr("tool_result.bubble.sender_main"), continuation.strip()))
        try:
            bubble.handoff_transparency = (
                bubble.handoff_transparency
                or self._should_use_handoff_transparency(bubble.participant_id)
            )
            self.start_existing(bubble)
        except Exception as e:
            # A failed startup must not leave a record that appears to be running.
            bubble.status = "error"
            bubble.error = tr("tool_result.bubble.resume_failed", error=e)
            self._store.mark_done(bubble)
            return ToolResult(tool_call_id="", content=bubble.error, is_error=True)

        added_cycles = bubble.max_cycles - previous_max_cycles
        instruction = tr("tool_result.bubble.resume_instruction") if continuation.strip() else ""
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.bubble.resumed",
                id=bubble.id,
                count=bubble.resume_count,
                before=previous_max_cycles,
                after=bubble.max_cycles,
                added=added_cycles,
                instruction=instruction,
            ),
        )

    def start_existing(self, bubble: Bubble) -> None:
        """Start a mini-loop for an already-created (or resumed) bubble."""
        import asyncio

        from coworker.agent.bubble_loop import BubbleMiniLoop

        if bubble.brain is None:
            raise RuntimeError(tr("tool_result.bubble.brain_missing", id=bubble.id))
        mini_loop = BubbleMiniLoop(
            bubble=bubble,
            brain=bubble.brain,
            tool_registry=self._full_registry,
            system_prompt=self._prompt_builder.build(),
            bubble_store=self._store,
            inbox_watcher=self._inbox,
            logs_dir=self._logs_dir,
            parent_log=self._parent_log,
            usage_stats=self._usage_stats,
            usage_logs_root=self._logs_dir,
            long_term=self._long_term,
            communicate=self._communicate,
        )
        bubble.task = asyncio.create_task(bind_locale(mini_loop.run), name=f"bubble-{bubble.id}")

    def _should_use_handoff_transparency(self, participant_id: str) -> bool:
        stream_transport = (
            self._communicate.live_stream_transport(participant_id)
            if self._communicate is not None
            else None
        )
        return self._handoff_matcher.matches(
            participant_id,
            stream_transport=stream_transport,
        )

    async def _inject_palaces(
        self, bubble: Bubble, resolved_palaces: list[Palace], goal: str
    ) -> None:
        """把挂载宫殿的内容注入泡泡的 forked_context，并记下 memory_tags 并集。

        注入顺序（均为 system 消息，追加在 forked_context 末尾）：
        1. 关键 skill（critical_skills）的完整 body —— 强加载，保证在场；
        2. 宫殿速记卡（body）—— 卡片里已列出 related_skills + 引导，泡泡按需 get_skill；
        3. 按宫殿 memory_tags 过滤召回的相关长期记忆，合并为一条 [宫殿记忆] 消息。
        """
        from coworker.core.types import Message as _Msg

        all_tags: list[str] = []
        crit_loaded: list[str] = []
        related_all: list[str] = []
        for palace in resolved_palaces:
            for tag in palace.memory_tags:
                if tag not in all_tags:
                    all_tags.append(tag)
            for rs in palace.related_skills:
                if rs not in related_all:
                    related_all.append(rs)

            # 1. 关键 skill 强加载
            if self._skill_loader is not None:
                for sname in palace.critical_skills:
                    skill = self._skill_loader.get(sname)
                    if skill is None:
                        continue
                    crit_loaded.append(sname)
                    bubble.forked_context.append(
                        _Msg(
                            role="system",
                            content=tr(
                                "tool_result.bubble.palace_skill",
                                palace=palace.name,
                                skill=sname,
                                body=skill.body,
                            ),
                        )
                    )

            # 2. 宫殿速记卡
            bubble.forked_context.append(
                _Msg(
                    role="system",
                    content=tr("tool_result.bubble.palace", palace=palace.name, body=palace.body),
                )
            )

        bubble.palace_tags = all_tags

        # 3. 按标签过滤召回长期记忆
        recalled: list[dict] = []
        if all_tags and self._long_term is not None and self._long_term._mem is not None:
            recall_msg, recalled = await self._recall_by_tags(goal, all_tags)
            if recall_msg:
                bubble.forked_context.append(recall_msg)

        # 记下注入摘要，供泡泡启动写日志时消费（此刻泡泡自身的日志尚未创建）。
        bubble.palace_injection = {
            "palaces": [p.name for p in resolved_palaces],
            "tags": all_tags,
            "critical_skills": crit_loaded,
            "related_skills": related_all,
            "recalled": recalled,
        }

    async def _recall_by_tags(
        self, goal: str, tags: list[str]
    ) -> tuple[Message | None, list[dict]]:
        """语义召回 goal 相关记忆，后置过滤保留 tags 有交集者。

        返回 ([宫殿记忆] 消息, 匹配到的记忆 dict 列表)；无匹配返回 (None, [])。
        匹配集同时用于写注入日志。
        """
        from coworker.core.types import Message as _Msg

        lt = self._long_term
        if lt is None or lt._mem is None:
            return None, []
        try:
            matched = await lt.query_by_tags(goal, tags, limit=8)
        except Exception:
            return None, []
        if not matched:
            return None, []
        lines = [tr("tool_result.bubble.palace_memory", tags=", ".join(tags))]
        for i, m in enumerate(matched, 1):
            lines.append(
                tr(
                    "tool_result.bubble.memory_item",
                    index=i,
                    id=m["id"],
                    category=m["category"],
                    content=m["content"],
                    relevance=f"{m['relevance']:.2f}",
                )
            )
        msg = _Msg(
            role="system",
            content="\n".join(lines),
            recalled_memory_ids=[m["id"] for m in matched],
        )
        return msg, matched


class BubbleCheckTool(Tool):
    def __init__(self, store: BubbleStore) -> None:
        self._store = store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="bubble_check",
            description="查询泡泡的当前状态、进度和结论预览。",
            parameters={
                "type": "object",
                "properties": {
                    "bubble_id": {"type": "string", "description": "泡泡 ID"},
                },
                "required": ["bubble_id"],
            },
        )

    async def execute(self, bubble_id: str, **_) -> ToolResult:
        bubble = self._store.get(bubble_id)
        if not bubble:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.bubble.missing", id=bubble_id),
                is_error=True,
            )
        lines = [
            tr(
                "tool_result.bubble.check_header",
                id=bubble.id,
                status=bubble.status,
                goal=bubble.goal,
                used=bubble.cycles_used,
                max=bubble.max_cycles,
                seconds=f"{bubble.elapsed_seconds():.1f}",
                messages=len(bubble.inner_messages),
            )
        ]
        if bubble.participant_id:
            lines.append(
                tr("tool_result.bubble.check_participant", participant=bubble.participant_id)
            )
        if bubble.conversation_id:
            lines.append(
                tr("tool_result.bubble.check_conversation", conversation=bubble.conversation_id)
            )
        if bubble.handoff_transparency:
            lines.append(tr("tool_result.bubble.check_transparency"))
        if bubble.palaces:
            lines.append(tr("tool_result.bubble.check_palaces", palaces=", ".join(bubble.palaces)))
        if bubble.provider or bubble.model:
            lines.append(
                tr("tool_result.bubble.check_model", provider=bubble.provider, model=bubble.model)
            )
        if bubble.checkpoint_count > 0:
            lines.append(tr("tool_result.bubble.check_checkpoints", count=bubble.checkpoint_count))
        if bubble.resume_count > 0:
            lines.append(tr("tool_result.bubble.check_resumes", count=bubble.resume_count))
        if bubble.status == "timeout" and bubble.finished_at is not None:
            window = self._store.timeout_resume_seconds
            remaining = window - max(0.0, (datetime.now() - bubble.finished_at).total_seconds())
            if window <= 0:
                lines.append(tr("tool_result.bubble.check_resume_disabled"))
            elif remaining > 0:
                lines.append(
                    tr("tool_result.bubble.check_resume_remaining", seconds=f"{remaining:.0f}")
                )
            else:
                lines.append(tr("tool_result.bubble.check_resume_expired"))
        if bubble.partial_results:
            last = bubble.partial_results[-1]
            preview = last[:300] + ("..." if len(last) > 300 else "")
            lines.append(tr("tool_result.bubble.check_latest_checkpoint", result=preview))
        if bubble.result:
            preview = bubble.result[:500]
            lines.append(
                tr(
                    "tool_result.bubble.check_result",
                    result=preview + ("..." if len(bubble.result) > 500 else ""),
                )
            )
        if bubble.error:
            lines.append(tr("tool_result.bubble.check_error", error=bubble.error))
        return ToolResult(tool_call_id="", content="\n".join(lines))


class BubbleSendTool(Tool):
    def __init__(self, store: BubbleStore, inbox: InboxWatcher) -> None:
        self._store = store
        self._inbox = inbox

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="bubble_send",
            description=(
                "向泡泡（target='bbl_xxxx'）或主线程（target='main'）发送消息。\n"
                "消息将在目标的下一轮执行时注入其上下文，带有 [来自主线] 标签。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "目标：泡泡 ID（如 'bbl_a1b2'）或 'main'",
                    },
                    "message": {
                        "type": "string",
                        "description": "发送的消息内容",
                    },
                },
                "required": ["target", "message"],
            },
        )

    async def execute(self, target: str, message: str, **_) -> ToolResult:
        from coworker.core.types import IncomingEvent

        if target == "main":
            await self._inbox.push(
                IncomingEvent(
                    participant_id="system",
                    content=tr("tool_result.bubble.from_main", message=message),
                    source="system",
                )
            )
            return ToolResult(tool_call_id="", content=tr("tool_result.bubble.pushed_main"))

        bubble = self._store.get(target)
        if not bubble:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.bubble.missing", id=target),
                is_error=True,
            )
        if bubble.is_terminal():
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.bubble.terminal", id=target, status=bubble.status),
                is_error=True,
            )
        await bubble.inbox.put((tr("tool_result.bubble.sender_main"), message))
        return ToolResult(tool_call_id="", content=tr("tool_result.bubble.sent", id=target))


class BubbleCancelTool(Tool):
    def __init__(self, store: BubbleStore) -> None:
        self._store = store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="bubble_cancel",
            description="取消正在运行的泡泡。",
            parameters={
                "type": "object",
                "properties": {
                    "bubble_id": {"type": "string", "description": "泡泡 ID"},
                },
                "required": ["bubble_id"],
            },
        )

    async def execute(self, bubble_id: str, **_) -> ToolResult:
        bubble = self._store.get(bubble_id)
        if not bubble:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.bubble.missing", id=bubble_id),
                is_error=True,
            )
        if bubble.is_terminal():
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.bubble.already_terminal",
                    id=bubble_id,
                    status=bubble.status,
                ),
            )
        if bubble.task:
            bubble.task.cancel()
        return ToolResult(tool_call_id="", content=tr("tool_result.bubble.cancelled", id=bubble_id))


class BubbleListTool(Tool):
    def __init__(self, store: BubbleStore) -> None:
        self._store = store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="bubble_list",
            description="列出所有活跃泡泡的状态概览。",
            parameters={"type": "object", "properties": {}, "required": []},
        )

    async def execute(self, **_) -> ToolResult:
        active = self._store.list_active()
        if not active:
            return ToolResult(tool_call_id="", content=tr("tool_result.bubble.none_active"))
        lines = [tr("tool_result.bubble.list_title", count=len(active))]
        for b in active:
            tags = []
            if b.participant_id:
                tags.append(tr("tool_result.bubble.list_participant", participant=b.participant_id))
            if b.conversation_id:
                tags.append(
                    tr("tool_result.bubble.list_conversation", conversation=b.conversation_id)
                )
            if b.handoff_transparency:
                tags.append(tr("tool_result.bubble.list_transparency"))
            if b.palaces:
                tags.append(tr("tool_result.bubble.list_palaces", palaces=",".join(b.palaces)))
            if b.provider or b.model:
                tags.append(tr("tool_result.bubble.list_model", provider=b.provider, model=b.model))
            if b.resume_count > 0:
                tags.append(tr("tool_result.bubble.list_resumes", count=b.resume_count))
            tag_str = f" | {' '.join(tags)}" if tags else ""
            lines.append(
                tr(
                    "tool_result.bubble.list_item",
                    id=b.id,
                    status=b.status,
                    used=b.cycles_used,
                    max=b.max_cycles,
                    seconds=f"{b.elapsed_seconds():.0f}",
                    tags=tag_str,
                    goal=b.goal[:60],
                )
            )
        return ToolResult(tool_call_id="", content="\n".join(lines))


class BubbleDoneTool(Tool):
    """Registered in the main registry solely for schema consistency / prefix-cache reuse.

    In bubble context _execute_tools intercepts 'bubble_done' before it reaches the
    registry, so this execute() is never called there. In main context it returns an
    error if the model mistakenly calls it.
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="bubble_done",
            description=(
                "提交泡泡结论。checkpoint=False（默认）：提交最终结论并终止泡泡，result 将合并到主线程上下文。"
                "checkpoint=True：打阶段检查点——把当前阶段结论推送给主线后继续执行，不终止泡泡；"
                "适用于分阶段执行、需要主线审核中间结果等场景。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "result": {
                        "type": "string",
                        "description": "本次结论（checkpoint=True 时为阶段结论，False 时为最终完整结论）",
                    },
                    "checkpoint": {
                        "type": "boolean",
                        "description": (
                            "是否打检查点：true=把阶段结论推送给主线后继续执行；"
                            "false（默认）=提交最终结论并终止泡泡"
                        ),
                        "default": False,
                    },
                },
                "required": ["result"],
            },
        )

    async def execute(self, result: str = "", **_) -> ToolResult:
        return ToolResult(
            tool_call_id="",
            content=tr("tool_result.bubble.done_wrong_context"),
            is_error=True,
        )
