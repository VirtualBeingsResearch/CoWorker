from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from coworker.agent.bubble_handoff import (
    BubbleHandoffMatcher,
)
from coworker.core.types import ToolResult
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
                content="创建泡泡需要 goal；续跑超时泡泡请提供 bubble_id。",
                is_error=True,
            )

        resolved_participant_id = (
            participant_id.strip() if isinstance(participant_id, str) else ""
        )
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
                content=f"未知 provider: {provider}",
                is_error=True,
            )
        if not model:
            return ToolResult(
                tool_call_id="",
                content=f"provider '{provider}' 未能解析出可用模型，请显式传入 model。",
                is_error=True,
            )
        if not provider_obj.supports_tool_use(model):
            return ToolResult(
                tool_call_id="",
                content=f"模型 '{model}' 不支持 provider '{provider}' 上的 tool use。",
                is_error=True,
            )

        # 解析并校验挂载的宫殿（在创建泡泡前，未知名直接报错）
        resolved_palaces = []
        if palaces:
            if self._palace_loader is None:
                return ToolResult(
                    tool_call_id="", content="未启用记忆宫殿，无法挂载 palaces。", is_error=True
                )
            for pname in palaces:
                palace = self._palace_loader.get(pname)
                if palace is None:
                    available = ", ".join(self._palace_loader.list_names()) or "（无）"
                    return ToolResult(
                        tool_call_id="",
                        content=f"记忆宫殿 '{pname}' 不存在。可用：{available}",
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
        bubble.handoff_transparency = self._should_use_handoff_transparency(
            bubble.participant_id
        )
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
                        content = f"当前线程已经分叉为泡泡 {bubble.id}。"
                    else:
                        content = f"工具 {tc_name} 在 bubble 分叉后由主线程执行，结果不在此处。"
                    bubble.forked_context.append(_Msg(
                        role="tool",
                        content=content,
                        tool_call_id=tc_id,
                    ))

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
        forked_tokens = sum(
            estimate_content_tokens(m.content) for m in bubble.forked_context
        )
        self.start_existing(bubble)

        context_desc = (
            f"全新上下文（仅含 {len(bubble.forked_context)} 条 pinned 消息）"
            if fresh_start
            else f"分叉上下文（{len(bubble.forked_context)} 条消息，约 {forked_tokens} tokens）"
        )
        palace_desc = (
            f"\n已挂宫殿：{', '.join(p.name for p in resolved_palaces)}"
            if resolved_palaces
            else ""
        )
        thinking_desc = "" if thinking else "\n模式：非思考（快速执行）"
        model_desc = f"\n模型：{bubble.provider}/{bubble.model}"
        communication_desc = ""
        if bubble.participant_id:
            communication_desc = f"\n通信绑定：{bubble.participant_id}"
            if bubble.conversation_id:
                communication_desc += f" / {bubble.conversation_id}"
            communication_desc += "（后续匹配消息会直接转交给此泡泡）"
            if bubble.handoff_transparency:
                communication_desc += "\n通信透明标识：已启用（转交、回复和结束会向对方说明）"
        return ToolResult(
            tool_call_id="",
            content=(
                f"泡泡已创建：id={bubble.id}\n"
                f"目标：{goal}\n"
                f"最大轮次：{max_cycles}\n"
                f"{context_desc}{palace_desc}{thinking_desc}{model_desc}{communication_desc}\n"
                f"使用 bubble_check('{bubble.id}') 查看进度，"
                f"bubble_send('{bubble.id}', '消息') 与其通信。"
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
                content="max_cycles 必须是整数。",
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
            await bubble.inbox.put(("主线", continuation.strip()))
        try:
            bubble.handoff_transparency = (
                bubble.handoff_transparency
                or self._should_use_handoff_transparency(bubble.participant_id)
            )
            self.start_existing(bubble)
        except Exception as e:
            # A failed startup must not leave a record that appears to be running.
            bubble.status = "error"
            bubble.error = f"续跑启动失败：{e}"
            self._store.mark_done(bubble)
            return ToolResult(tool_call_id="", content=bubble.error, is_error=True)

        added_cycles = bubble.max_cycles - previous_max_cycles
        instruction = "已附加主线续跑指令。" if continuation.strip() else ""
        return ToolResult(
            tool_call_id="",
            content=(
                f"泡泡 {bubble.id} 已恢复并继续执行（第 {bubble.resume_count} 次续跑）。"
                f"累计轮次预算 {previous_max_cycles} → {bubble.max_cycles}（新增 {added_cycles} 轮）。"
                f"{instruction}"
            ),
        )

    def start_existing(self, bubble: Bubble) -> None:
        """Start a mini-loop for an already-created (or resumed) bubble."""
        import asyncio

        from coworker.agent.bubble_loop import BubbleMiniLoop

        if bubble.brain is None:
            raise RuntimeError(f"泡泡 {bubble.id} 缺少独立 brain，无法启动。")
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
        bubble.task = asyncio.create_task(mini_loop.run(), name=f"bubble-{bubble.id}")

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
                    bubble.forked_context.append(_Msg(
                        role="system",
                        content=f"[宫殿:{palace.name} · skill:{sname}]\n{skill.body}",
                    ))

            # 2. 宫殿速记卡
            bubble.forked_context.append(_Msg(
                role="system",
                content=f"[宫殿:{palace.name}]\n{palace.body}",
            ))

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
        lines = [f"[宫殿记忆] 以下长期记忆与当前宫殿（标签：{', '.join(tags)}）和任务相关："]
        for i, m in enumerate(matched, 1):
            lines.append(f"{i}. id={m['id']} [{m['category']}] {m['content']}（相关度：{m['relevance']:.2f}）")
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
            return ToolResult(tool_call_id="", content=f"未找到泡泡 [{bubble_id}]", is_error=True)
        lines = [
            f"泡泡 {bubble.id}",
            f"状态：{bubble.status}",
            f"目标：{bubble.goal}",
            f"当前执行轮次：{bubble.cycles_used}/{bubble.max_cycles}",
            f"运行时长：{bubble.elapsed_seconds():.1f}s",
            f"内部消息数：{len(bubble.inner_messages)}",
        ]
        if bubble.participant_id:
            lines.append(f"服务对象：{bubble.participant_id}")
        if bubble.conversation_id:
            lines.append(f"服务会话：{bubble.conversation_id}")
        if bubble.handoff_transparency:
            lines.append("通信透明标识：已启用")
        if bubble.palaces:
            lines.append(f"挂载宫殿：{', '.join(bubble.palaces)}")
        if bubble.provider or bubble.model:
            lines.append(f"模型：{bubble.provider}/{bubble.model}")
        if bubble.checkpoint_count > 0:
            lines.append(f"检查点次数：{bubble.checkpoint_count}")
        if bubble.resume_count > 0:
            lines.append(f"超时续跑次数：{bubble.resume_count}")
        if bubble.status == "timeout" and bubble.finished_at is not None:
            window = self._store.timeout_resume_seconds
            remaining = window - max(
                0.0, (datetime.now() - bubble.finished_at).total_seconds()
            )
            if window <= 0:
                lines.append("超时续跑：已禁用")
            elif remaining > 0:
                lines.append(
                    f"超时续跑：剩余约 {remaining:.0f}s，可调用 bubble_spawn(bubble_id=...) 继续。"
                )
            else:
                lines.append("超时续跑：宽限期已过")
        if bubble.partial_results:
            last = bubble.partial_results[-1]
            preview = last[:300] + ("..." if len(last) > 300 else "")
            lines.append(f"最近阶段结论：{preview}")
        if bubble.result:
            preview = bubble.result[:500]
            lines.append(f"结论预览：{preview}{'...' if len(bubble.result) > 500 else ''}")
        if bubble.error:
            lines.append(f"错误：{bubble.error}")
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
            await self._inbox.push(IncomingEvent(
                participant_id="system",
                content=f"[来自主线] {message}",
                source="system",
            ))
            return ToolResult(tool_call_id="", content="消息已推送到主线程 inbox。")

        bubble = self._store.get(target)
        if not bubble:
            return ToolResult(tool_call_id="", content=f"未找到泡泡 [{target}]", is_error=True)
        if bubble.is_terminal():
            return ToolResult(
                tool_call_id="",
                content=f"泡泡 {target} 已终止（{bubble.status}），无法接收消息。",
                is_error=True,
            )
        await bubble.inbox.put(("主线", message))
        return ToolResult(tool_call_id="", content=f"消息已发送到泡泡 {target}。")


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
            return ToolResult(tool_call_id="", content=f"未找到泡泡 [{bubble_id}]", is_error=True)
        if bubble.is_terminal():
            return ToolResult(
                tool_call_id="",
                content=f"泡泡 {bubble_id} 已处于终态 {bubble.status}，无需取消。",
            )
        if bubble.task:
            bubble.task.cancel()
        return ToolResult(tool_call_id="", content=f"已发送取消信号到泡泡 {bubble_id}。")


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
            return ToolResult(tool_call_id="", content="当前没有活跃的泡泡。")
        lines = [f"活跃泡泡 {len(active)} 个："]
        for b in active:
            tags = []
            if b.participant_id:
                tags.append(f"对象={b.participant_id}")
            if b.conversation_id:
                tags.append(f"会话={b.conversation_id}")
            if b.handoff_transparency:
                tags.append("透明标识=开")
            if b.palaces:
                tags.append(f"宫殿={','.join(b.palaces)}")
            if b.provider or b.model:
                tags.append(f"模型={b.provider}/{b.model}")
            if b.resume_count > 0:
                tags.append(f"续跑={b.resume_count}")
            tag_str = f" | {' '.join(tags)}" if tags else ""
            lines.append(
                f"  [{b.id}] {b.status} | {b.cycles_used}/{b.max_cycles}轮 "
                f"| {b.elapsed_seconds():.0f}s{tag_str} | 目标：{b.goal[:60]}"
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
            content="bubble_done 只能在泡泡模式中调用。",
            is_error=True,
        )
