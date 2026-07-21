from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from coworker.agent.bubble_handoff import (
    BubbleHandoffNotifier,
    bubble_reply_fallback_prefix,
    bubble_reply_message_extra,
)
from coworker.agent.incoming_content import build_content_blocks
from coworker.core.types import IncomingEvent, Message, SummaryResult

if TYPE_CHECKING:
    from coworker.agent.bubble import Bubble, BubbleStore
    from coworker.agent.inbox_watcher import InboxWatcher
    from coworker.agent.interaction_log import InteractionLogger
    from coworker.agent.usage_stats import UsageStatsCollector
    from coworker.brain.brain import Brain
    from coworker.core.tool_scope import ToolScope
    from coworker.memory.long_term import LongTermMemory
    from coworker.memory.short_term import ShortTermMemory
    from coworker.tools.communicate_tool import CommunicateTool
    from coworker.tools.reasoning_tools import TaskStore
    from coworker.tools.registry import ToolRegistry


# 所有泡泡共有的工具拦截：{工具名: 拦截原因}。
# 泡泡是有 cycle 上限的目标线程，空转类工具只会白白烧光轮次；另有一些工具会改动父线程状态。
# 这些工具对泡泡内的 LLM 不可见，即便被调用也直接返回原因。
_BUBBLE_BASE_INTERCEPTS: dict[str, str] = {
    # 空转 / 无意义等待——泡泡轮次有限，休眠只会白白烧光轮次。
    # 注：breathe 是仪式性过渡而非空转，对泡泡仍有用，保留可调用。
    "sleep": "泡泡是有轮次上限的目标线程，休眠只会白白烧光轮次。请直接推进目标，完成后调用 bubble_done(result=...)。",
    # 会影响父线程状态——泡泡不应触碰（与身份 prompt 的告诫一致）
    "restart_self": "该工具会重启父线程，泡泡内不可用。请专注自身目标。",
    "clear_short_term_memory": "该工具会压缩父线程短期记忆，泡泡内不可用。",
    "compress_memory": "该工具会压缩父线程记忆，泡泡内不可用。",
}


class BubbleMiniLoop:
    _CYCLES_HARD_CAP = 50

    def __init__(
        self,
        bubble: Bubble,
        brain: Brain,
        tool_registry: ToolRegistry,
        system_prompt: str,
        bubble_store: BubbleStore,
        inbox_watcher: InboxWatcher,
        logs_dir: str = "data/logs",
        parent_log: InteractionLogger | None = None,
        usage_stats: UsageStatsCollector | None = None,
        usage_logs_root: str | Path | None = None,
        task_store: TaskStore | None = None,
        long_term: LongTermMemory | None = None,
        communicate: CommunicateTool | None = None,
    ) -> None:
        self._bubble = bubble
        self._brain = brain
        self._tools = tool_registry
        self._system_prompt = system_prompt
        self._store = bubble_store
        self._inbox_watcher = inbox_watcher
        self._logs_dir = logs_dir
        self._parent_log = parent_log
        self._usage_stats = usage_stats
        self._usage_logs_root = Path(usage_logs_root) if usage_logs_root is not None else Path(logs_dir)
        self._long_term = long_term
        self._communicate = communicate
        self._handoff_notifier = BubbleHandoffNotifier(communicate)
        # 默认 None：泡泡用一次性内存 TaskStore，任务不外泄到主线。
        # 注入真实 task_store 时（如潜意识 introspect），泡泡内 task_create 直写主线持久任务。
        self._task_store_override = task_store
        self._stm: ShortTermMemory | None = None
        self._scope: ToolScope | None = None
        self._ilog: InteractionLogger | None = None
        self._log_path: Path | None = None

    @property
    def _short_term(self) -> ShortTermMemory:
        if self._stm is None:
            raise RuntimeError("Bubble short-term memory is not initialized")
        return self._stm

    async def run(self) -> None:
        bubble = self._bubble
        try:
            await self._handoff_notifier.announce_started(
                bubble,
                resumed=bubble.resume_count > 0,
            )
            await self._run_inner()
        except asyncio.CancelledError:
            bubble.status = "cancelled"
            bubble.error = "泡泡被手动取消"
            logger.info(f"Bubble {bubble.id} cancelled")
            raise
        except Exception as e:
            bubble.status = "error"
            bubble.error = str(e)
            logger.exception(f"Bubble {bubble.id} failed: {e}")
        finally:
            fork_plus_identity = len(bubble.forked_context) + 1
            msgs = self._stm.primary if self._stm is not None else []
            bubble.inner_messages = msgs[fork_plus_identity:]
            if self._stm is not None:
                # A resumed bubble needs its own pinned state as well as its visible
                # transcript; pin/unpin changes made inside a bubble are otherwise
                # lost when its short-term memory is rebuilt.
                bubble.pinned_items = list(self._stm.pinned_items)
            self._cleanup_scope()
            await self._persist_log()
            self._mark_usage_log_complete()
            await self._auto_merge()

    def _tool_intercepts(self) -> dict[str, str]:
        """Tools to intercept in this loop: {name: reason}. Override to extend."""
        intercepts = dict(_BUBBLE_BASE_INTERCEPTS)
        if not self._bubble.participant_id:
            intercepts["communicate"] = (
                "该泡泡未绑定通信对象，不能直接对外通信。"
                "如需父线程协助，请调用 bubble_send(target='main', message=...)。"
            )
        return intercepts

    def _log_filename(self, bubble: Bubble) -> str:
        """泡泡日志文件名。子类可覆写以在文件名中体现类型（如潜意识模式）。"""
        return f"{bubble.id}.jsonl"

    def _build_identity_content(self, bubble: Bubble) -> str:
        if bubble.participant_id:
            conversation = (
                f"，会话为 {bubble.conversation_id}"
                if bubble.conversation_id
                else ""
            )
            external_communication = (
                f"- 此泡泡已绑定通信对象 {bubble.participant_id}{conversation}。"
                "该对象的新消息会直接转交给你；"
                "需要回复时可调用 communicate(message=...)，系统会固定投递给该对象。"
                "不得借此联系其他对象。\n"
            )
            if bubble.handoff_transparency:
                external_communication += (
                    "- 系统会自动标识你的直接回复，并在 Bubble 开始和结束时通知对方。\n"
                )
        else:
            external_communication = (
                "- 此泡泡未绑定外部通信对象，不能直接通过通信信道联系用户；"
                "如需通知，通过 bubble_send(target='main') 转交父线程。\n"
            )
        return (
            f"[泡泡模式]\n"
            f"你现在正处在一个独立的并行思考线程（泡泡）中，泡泡 id 为 {bubble.id}，目标：{bubble.goal}。\n"
            f"最多执行 {bubble.max_cycles} 轮，完成后调用 bubble_done(result='...') 提交结论。\n"
            f"\n"
            f"【重要认知】\n"
            f"- 当前泡泡的所有思考过程、工具调用记录、中间结论，父线程均不可见。\n"
            f"- 父线程唯一能看到的是你通过 bubble_done 提交的 result 字段，以及 bubble_send 主动发送的消息。\n"
            f"- 因此 result 必须自包含、完整——包含关键数据、推导过程摘要和最终结论，不能假设父线程知道你做了什么。\n"
            f"\n"
            f"【通信】\n"
            f"- 用 bubble_send(target='main', message=...) 向父线程发送中间进展或需要协调的内容。\n"
            f"- 用 bubble_send(target='bbl_xxxx', message=...) 与其他泡泡通信。\n"
            f"- 用 bubble_done(result='...', checkpoint=True) 向主线汇报阶段结论并继续执行；"
            f"主线可通过 bubble_send 回复，下一轮自动注入。\n"
            f"  用 bubble_done(result='...') 提交最终结论并结束。\n"
            f"\n"
            f"【注意事项】\n"
            f"{external_communication}"
            f"- task_create/execute_code 等工具使用独立作业存储，不影响父线程，但文件系统共享，"
            f"执行代码时建议通过 cwd 参数指定独立工作目录；若需要等待代码结果再继续，"
            f"可对 execute_code 传 block=true，通常比反复 get_code_result 更省轮次。\n"
            f"- 不要调用 restart_self、clear_short_term_memory、compress_memory 等影响父线程状态的工具。"
        )

    async def _run_inner(self) -> None:
        from coworker.agent.interaction_log import InteractionLogger
        from coworker.core.tool_scope import ToolScope
        from coworker.memory.short_term import ShortTermMemory
        from coworker.tools.code_tools import BackgroundJobStore
        from coworker.tools.reasoning_tools import TaskStore

        bubble = self._bubble
        max_cycles = min(bubble.max_cycles, self._CYCLES_HARD_CAP)

        self._stm = ShortTermMemory()
        self._stm.primary = list(bubble.forked_context)
        self._stm.pinned_items = list(bubble.pinned_items)
        if bubble.forked_tree is not None:
            self._stm.tree.nodes = list(bubble.forked_tree.nodes)
        identity_content = self._build_identity_content(bubble)
        self._stm.primary.append(Message(role="system", content=identity_content))
        resume_notice = ""
        if bubble.inner_messages:
            # Timeout recovery keeps the original fork boundary and inserts a fresh
            # identity message before the old transcript, so tool-call/result order
            # remains valid for the provider.
            self._stm.primary.extend(bubble.inner_messages)
        if bubble.resume_count:
            resume_notice = (
                f"[系统] 泡泡此前达到轮次上限，现已第 {bubble.resume_count} 次续跑。"
                f"累计轮次预算已扩展至 {max_cycles} 轮，请基于已有上下文继续推进；"
                "若任务已完成，请调用 bubble_done(result=...) 提交最终结论。"
            )
            self._stm.primary.append(Message(role="user", content=resume_notice))

        log_path = Path(self._logs_dir) / "bubbles" / self._log_filename(bubble)
        self._log_path = log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ilog = InteractionLogger(str(log_path))
        self._ilog = ilog
        self._brain.add_summary_usage_listener(
            lambda response, meta: ilog.log_summary_llm_response(
                provider=response.provider,
                model=response.model,
                usage=response.usage,
                context_hint=str(meta.get("context_hint") or ""),
            )
        )
        self._brain.add_vision_usage_listener(
            lambda response, meta: ilog.log_vision_llm_response(
                provider=response.provider,
                model=response.model,
                usage=response.usage,
                label=str(meta.get("label") or ""),
            )
        )
        usage_stats = self._usage_stats
        if usage_stats is not None:
            from coworker.agent.usage_stats import UsageStatsCollector

            stream_id = UsageStatsCollector.bubble_stream_id(self._usage_logs_root, log_path)

            def record_usage(entry: dict) -> None:
                usage_stats.on_entry(entry, stream_id=stream_id)

            ilog.add_listener(record_usage)
        ilog.log_message_in(participant_id="system", content=identity_content, source="bubble")
        if resume_notice:
            ilog.log_message_in(participant_id="system", content=resume_notice, source="bubble")
        if bubble.palace_injection:
            inj = bubble.palace_injection
            ilog.log_palace_injection(
                palaces=inj.get("palaces", []),
                tags=inj.get("tags", []),
                critical_skills=inj.get("critical_skills", []),
                related_skills=inj.get("related_skills", []),
                recalled=inj.get("recalled", []),
            )

        self._scope = ToolScope(
            task_store=self._task_store_override or TaskStore(store_path=None),
            job_store=BackgroundJobStore(),
            inbox=None,
            scope_id=bubble.id,
            allow_block=True,
            brain=bubble.brain,
            short_term=self._stm,
            communicate_participant_id=bubble.participant_id,
            communicate_conversation_id=bubble.conversation_id,
            communicate_message_prefix=(
                bubble_reply_fallback_prefix(bubble.participant_id)
                if bubble.handoff_transparency
                else ""
            ),
            communicate_message_extra=(
                bubble_reply_message_extra(bubble.id)
                if (
                    bubble.handoff_transparency
                    and self._communicate is not None
                    and self._communicate.supports_message_extra(bubble.participant_id)
                )
                else {}
            ),
        )
        scoped_tools = self._tools.scoped(self._scope)
        intercepts = self._tool_intercepts()
        if intercepts:
            scoped_tools = scoped_tools.intercept(intercepts)

        # max_cycles is a cumulative bubble budget.  On a resume, continue from the
        # cycles that were already consumed instead of restarting the counter.
        cycle = bubble.cycles_used
        while cycle < max_cycles:
            bubble.cycles_used = cycle + 1
            if bubble.is_terminal():
                break

            await self._drain_inbox()
            self._warn_if_bursting(cycle, max_cycles)
            self._stm.reinject_missing_pins()
            tool_schemas = scoped_tools.get_schemas(
                model_has_vision=self._brain.current_model_has_vision
            )

            if self._ilog:
                self._ilog.log_thinking_start(cycle, thinking=bool(self._brain.thinking))
            response = await self._brain.think(
                messages=self._stm.build_context(),
                system_prompt=self._system_prompt,
                tools=tool_schemas,
            )

            if self._ilog:
                self._ilog.log_llm_response(
                    reasoning_content=response.reasoning_content,
                    content=response.content,
                    tool_calls=response.tool_calls,
                    stop_reason=response.stop_reason,
                    model=response.model,
                    usage=response.usage,
                    provider=self._brain.current_provider_name,
                    thinking=bool(self._brain.thinking),
                )

            self._stm.primary.append(Message(
                role="assistant",
                content=response.content,
                reasoning_content=response.reasoning_content,
                tool_calls=[
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in response.tool_calls
                ],
                stop_reason=response.stop_reason,
            ))

            if not response.tool_calls:
                nudge = "[系统] 请继续完成任务，或调用 bubble_done(result=...) 提交最终结论。"
                self._stm.primary.append(Message(role="user", content=nudge))
                if self._ilog:
                    self._ilog.log_message_in(participant_id="system", content=nudge, source="bubble")
                cycle += 1
                max_cycles = min(bubble.max_cycles, self._CYCLES_HARD_CAP)
                continue

            done = await self._execute_tools(response.tool_calls, scoped_tools)
            if done:
                break

            cycle += 1
            max_cycles = min(bubble.max_cycles, self._CYCLES_HARD_CAP)

        if not bubble.is_terminal() and cycle >= max_cycles:
            await self._auto_summarize()

    async def _drain_inbox(self) -> None:
        bubble = self._bubble
        while not bubble.inbox.empty():
            try:
                item = bubble.inbox.get_nowait()
                if isinstance(item, IncomingEvent):
                    self._short_term.primary.append(Message(
                        role="user",
                        content=build_content_blocks([item]),
                        source=item.source,
                    ))
                    if self._ilog:
                        self._ilog.log_message_in(
                            participant_id=item.participant_id,
                            content=item.content,
                            source=item.source,
                            attachments=item.attachments or None,
                            conversation_id=item.conversation_id,
                        )
                else:
                    sender_id, message_text = item
                    content = f"[来自 {sender_id}] {message_text}"
                    self._short_term.primary.append(Message(role="user", content=content))
                    if self._ilog:
                        self._ilog.log_message_in(
                            participant_id=sender_id,
                            content=message_text,
                            source="bubble",
                        )
            except asyncio.QueueEmpty:
                break

    def _warn_if_bursting(self, cycle: int, max_cycles: int) -> None:
        # 进入最后一轮前给出预警：泡泡即将破灭，给它机会主动收尾、保存工作并通知主线，
        # 而不是被 _auto_summarize 强制摘要（可能丢失关键细节）。
        if cycle != max_cycles - 1:
            return
        warning = (
            "[系统预警] 泡泡即将破灭。\n"
            "请立即调用 bubble_done(result=...) 提交完整、自包含的结论, 如果当前工作未完成或者马上要完成了, 请调用bubble_done(result=..., checkpoint=True)提交中间结论, 然后再继续推进当前工作——"
            "result 会自动转交主线，无需再用 bubble_send 重复发送结论。\n"
            "仅当另有需要主线立即处理、且不宜放进结论的事项时，"
            "才在 bubble_done 之前先 bubble_send(target='main', message=...)。\n"
            "若本轮未调用 bubble_done，系统将自动摘要你的思考过程，可能丢失关键细节。"
        )
        self._short_term.primary.append(Message(role="user", content=warning))
        if self._ilog:
            self._ilog.log_message_in(participant_id="system", content=warning, source="bubble")

    async def _execute_tools(self, tool_calls, scoped_tools) -> bool:
        bubble = self._bubble

        for tc in tool_calls:
            if self._ilog:
                self._ilog.log_tool_call(id=tc.id, name=tc.name, arguments=tc.arguments)

            is_error = False
            if tc.name == "bubble_done":
                result_text = tc.arguments.get("result", "")
                if tc.arguments.get("checkpoint", False):
                    bubble.partial_results.append(result_text)
                    bubble.checkpoint_count += 1
                    checkpoint_msg = (
                        f"[泡泡检查点] id={bubble.id} | 第{bubble.checkpoint_count}次\n"
                        f"阶段结论：{result_text}\n"
                        f"泡泡继续执行中。如需反馈可通过 bubble_send('{bubble.id}', ...) 发送。"
                    )
                    await self._handle_send("main", checkpoint_msg)
                    extension = self._extend_cycle_budget_for_checkpoint()
                    if extension > 0:
                        content = f"检查点已发送给主线（第{bubble.checkpoint_count}次），已自动续期 {extension} 轮，继续执行。"
                    else:
                        content = f"检查点已发送给主线（第{bubble.checkpoint_count}次），继续执行。"
                else:
                    bubble.result = result_text
                    bubble.status = "done"
                    content = f"泡泡 {bubble.id} 已完成，结论已记录。"
            elif tc.name == "bubble_send":
                content = await self._handle_send(
                    tc.arguments.get("target", ""), tc.arguments.get("message", "")
                )
            else:
                result = await scoped_tools.execute(tc)
                content = result.content if isinstance(result.content, str) else str(result.content)
                is_error = result.is_error

            if self._ilog:
                self._ilog.log_tool_result(id=tc.id, name=tc.name, content=content, is_error=is_error)
            self._short_term.primary.append(Message(role="tool", content=content, tool_call_id=tc.id))

            if bubble.is_terminal():
                break

        return bubble.is_terminal()

    def _extend_cycle_budget_for_checkpoint(self) -> int:
        bubble = self._bubble
        remaining = self._CYCLES_HARD_CAP - bubble.max_cycles
        if remaining <= 0:
            return 0
        extension = min(bubble.initial_max_cycles, remaining)
        bubble.max_cycles += extension
        return extension

    def _cleanup_scope(self) -> None:
        if self._scope is None:
            return
        from coworker.tools.code_tools import _kill_tree
        job_store = self._scope.job_store
        if job_store._cleanup_task and not job_store._cleanup_task.done():
            job_store._cleanup_task.cancel()
        for job in list(job_store._jobs.values()):
            if job.status == "running" and job.process is not None:
                with contextlib.suppress(Exception):
                    _kill_tree(job.process.pid)

    async def _handle_send(self, target: str, message_text: str) -> str:
        from coworker.core.types import IncomingEvent

        bubble = self._bubble
        sender_id = bubble.id

        if target == "main":
            await self._inbox_watcher.push(IncomingEvent(
                participant_id=sender_id,
                content=message_text,
                source="bubble",
            ))
            return "消息已发送到主线程。"

        target_bubble = self._store.get(target)
        if not target_bubble:
            return f"目标泡泡 '{target}' 未找到或已完成。"
        if target_bubble.is_terminal():
            return f"目标泡泡 '{target}' 已终止，无法接收消息。"
        await target_bubble.inbox.put((sender_id, message_text))
        return f"消息已发送到泡泡 {target}。"

    async def _auto_summarize(self) -> None:
        bubble = self._bubble
        bubble.status = "timeout"
        fork_plus_identity = len(bubble.forked_context) + 1
        inner_msgs = self._short_term.primary[fork_plus_identity:]
        try:
            raw = await self._brain.summarize(
                inner_msgs,
                context_hint=f"泡泡目标：{bubble.goal}",
            )
            summary = raw.content if isinstance(raw, SummaryResult) else raw
            try:
                parsed = json.loads(summary)
                bubble.result = parsed.get("summary", summary)
            except Exception:
                bubble.result = summary
        except Exception as e:
            bubble.result = f"（自动摘要失败：{e}）"
        logger.info(f"Bubble {bubble.id} timed out after {bubble.max_cycles} cycles")

    async def _persist_log(self) -> None:
        if self._ilog is None:
            return
        bubble = self._bubble
        try:
            self._ilog._write({
                "__meta__": True,
                "id": bubble.id,
                "goal": bubble.goal,
                "provider": bubble.provider,
                "model": bubble.model,
                "status": bubble.status,
                "cycles_used": bubble.cycles_used,
                "max_cycles": bubble.max_cycles,
                "elapsed_seconds": bubble.elapsed_seconds(),
                "error": bubble.error,
                "participant_id": bubble.participant_id,
                "conversation_id": bubble.conversation_id,
                "handoff_transparency": bubble.handoff_transparency,
                "palaces": bubble.palaces,
                "palace_tags": bubble.palace_tags,
                "resume_count": bubble.resume_count,
                "last_resumed_at": (
                    bubble.last_resumed_at.isoformat() if bubble.last_resumed_at else None
                ),
            })
        except Exception as e:
            logger.warning(f"Failed to persist bubble log for {bubble.id}: {e}")

    def _mark_usage_log_complete(self) -> None:
        if self._usage_stats is None or self._log_path is None:
            return
        self._usage_stats.mark_bubble_log_complete(self._usage_logs_root, self._log_path)

    async def _write_back_to_palaces(self) -> None:
        """挂宫殿的泡泡成功收尾时，把结论按宫殿 memory_tags 写回长期记忆。

        确定性钩子，不依赖模型自觉，也不改宫殿卡片正文——写回落到 mem0，
        靠 bubble_spawn 的标签召回在下次挂同一宫殿时自动捞回。
        """
        bubble = self._bubble
        if bubble.status != "done" or not bubble.palace_tags or not bubble.result.strip():
            return
        if self._long_term is None or self._long_term._mem is None:
            return
        try:
            await self._long_term.write(
                content=bubble.result,
                category="experience",
                tags=list(bubble.palace_tags),
            )
            logger.debug(f"Bubble {bubble.id} result written back to palace tags {bubble.palace_tags}")
        except Exception as e:
            logger.warning(f"Bubble {bubble.id} palace write-back failed: {e}")

    async def _auto_merge(self) -> None:
        from coworker.core.types import IncomingEvent

        bubble = self._bubble
        await self._write_back_to_palaces()
        await self._handoff_notifier.announce_finished(bubble)
        merge_msg = _build_merge_message(bubble)
        if bubble.status == "timeout" and self._store.timeout_resume_seconds > 0:
            merge_msg += (
                f"\n\n[续跑提示] 若任务仍未完成，可在 {self._store.timeout_resume_seconds}s 内调用 "
                f"bubble_spawn(bubble_id='{bubble.id}', goal='续跑指令', max_cycles=...) "
                "保留上下文继续执行。"
            )
        # Push through inbox instead of direct append so the merge message is
        # inserted at the start of the next main-loop cycle, never mid-tool-execution.
        # 注：不要在这里再 log_message_in——主循环排空 inbox 时会对每条 event 记一次
        # （loop._cycle），而 _parent_log 与主循环 _ilog 是同一个 logger，显式补记会双写。
        await self._inbox_watcher.push(IncomingEvent(
            participant_id=bubble.id,
            content=merge_msg,
            source="bubble",
        ))
        # A participant can send a follow-up while the bubble is in its final
        # model/tool cycle.  The bubble is terminal by this point, so return
        # any such undrained external message to the main inbox instead of
        # silently losing it.
        while not bubble.inbox.empty():
            try:
                item = bubble.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, IncomingEvent):
                await self._inbox_watcher.push(item)
        self._store.mark_done(bubble)

def _build_merge_message(bubble: Bubble) -> str:
    status_label = {
        "done": "成功完成",
        "timeout": "达到最大轮次后摘要",
        "error": "执行出错",
        "cancelled": "已取消",
    }.get(bubble.status, bubble.status)

    lines = [
        f"[泡泡思考结果] id={bubble.id} | 状态={status_label} | "
        f"执行 {bubble.cycles_used} 轮 | 耗时 {bubble.elapsed_seconds():.1f}s",
        f"目标：{bubble.goal}",
    ]
    if bubble.participant_id:
        lines.append(f"通信对象：{bubble.participant_id}")
    if bubble.conversation_id:
        lines.append(f"通信会话：{bubble.conversation_id}")
    if bubble.checkpoint_count > 0:
        lines.append(f"检查点次数：{bubble.checkpoint_count}")
    if bubble.resume_count > 0:
        lines.append(f"超时续跑次数：{bubble.resume_count}")
    lines.append("---")

    cycle_summaries: list[str] = []
    cycle_num = 1
    for msg in bubble.inner_messages:
        if msg.role == "assistant" and msg.tool_calls:
            tool_names = [tc.get("function", {}).get("name", "?") for tc in msg.tool_calls]
            cycle_summaries.append(f"- 轮次{cycle_num}：调用 {', '.join(tool_names)}")
            cycle_num += 1
    if cycle_summaries:
        lines.append("思考过程：")
        lines.extend(cycle_summaries[:10])
        if len(cycle_summaries) > 10:
            lines.append(f"  ...（共 {len(cycle_summaries)} 轮）")
        lines.append("---")

    if bubble.partial_results:
        lines.append(f"阶段结论（共{len(bubble.partial_results)}个检查点）：")
        for i, pr in enumerate(bubble.partial_results, 1):
            preview = pr[:200] + ("..." if len(pr) > 200 else "")
            lines.append(f"  第{i}次：{preview}")
        lines.append("---")
    if bubble.error:
        lines.append(f"错误信息：{bubble.error}")
    lines.append(f"结论：\n{bubble.result}" if bubble.result else "（无结论）")

    return "\n".join(lines)
