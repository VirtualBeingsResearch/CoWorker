from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from coworker.agent.incoming_content import build_content_blocks
from coworker.core.autonomy import (
    AutonomyBlockedError,
    AutonomyController,
    AutonomyLevel,
    AutonomyScope,
    replace_autonomy_trigger,
)
from coworker.core.constants import TICK_TAG
from coworker.core.exceptions import RestartRequestedException
from coworker.core.types import AgentState, IncomingEvent, Message, ToolResult
from coworker.i18n import tr
from coworker.memory.recent_activity import render_recent_activity_replay
from coworker.tools.reasoning_tools import format_task_times

if TYPE_CHECKING:
    from coworker.agent.bubble import BubbleStore
    from coworker.agent.inbox_watcher import InboxWatcher
    from coworker.agent.interaction_log import InteractionLogger
    from coworker.agent.subconscious import SubconsciousScheduler
    from coworker.brain.brain import Brain
    from coworker.core.config import Config
    from coworker.identity.identity import Identity
    from coworker.memory.long_term import LongTermMemory
    from coworker.memory.recent_activity import RecentActivityMemory
    from coworker.memory.short_term import ShortTermMemory
    from coworker.prompts.system_prompt import SystemPromptBuilder
    from coworker.tools.reasoning_tools import TaskStore
    from coworker.tools.registry import ToolRegistry

# 连续错误阈值：超过此数量的连续错误将触发恢复措施
_MAX_CONSECUTIVE_ERRORS = 5
# 恢复后的等待时间（秒），给 API 冷却时间
_RECOVERY_COOLDOWN_SECONDS = 30


class AgentLoop:
    def __init__(
        self,
        brain: Brain,
        short_term: ShortTermMemory,
        long_term: LongTermMemory,
        tool_registry: ToolRegistry,
        identity: Identity,
        prompt_builder: SystemPromptBuilder,
        inbox_watcher: InboxWatcher,
        config: Config,
        interaction_log: InteractionLogger | None = None,
        state: AgentState | None = None,
        snapshot_path: Path | None = None,
        task_store: TaskStore | None = None,
        task_reminder_interval: int = 10,
        task_reminder_seconds: float = 300,
        bubble_store: BubbleStore | None = None,
        subconscious: SubconsciousScheduler | None = None,
        recent_activity: RecentActivityMemory | None = None,
        autonomy: AutonomyController | None = None,
        continuation_trigger: AutonomyLevel | None = None,
        rebalance_on_activation: bool = False,
    ) -> None:
        self._brain = brain
        self._short_term = short_term
        self._long_term = long_term
        self._tools = tool_registry
        self._identity = identity
        self._prompt_builder = prompt_builder
        self._inbox = inbox_watcher
        self._config = config
        self._ilog = interaction_log
        self._stop_event = asyncio.Event()
        self._snapshot_path = snapshot_path
        self._consecutive_errors = 0
        self._task_store = task_store
        self._task_reminder_interval = task_reminder_interval
        self._task_reminder_seconds = task_reminder_seconds
        self._last_task_reminder_cycle = 0
        self._last_task_reminder_time = time.monotonic()
        self._bubble_store = bubble_store
        self._subconscious = subconscious
        self._recent_activity = recent_activity
        self._autonomy = autonomy or AutonomyController(
            config.agent.autonomy_level,
            config.agent.autonomy_thresholds,
        )
        self._continuation_trigger = continuation_trigger
        self._rebalance_on_activation = rebalance_on_activation
        self._last_compress_generation = short_term.compress_generation
        self.state = state or AgentState(
            current_provider=brain.current_provider_name,
            current_model=brain.current_model,
        )

    def current_system_prompt(self) -> str:
        return self._prompt_builder.build()

    def refresh_system_prompt(self) -> None:
        self._prompt_builder.refresh()

    async def run(self) -> None:
        self.state.is_running = True
        logger.info("AgentLoop started")

        watcher: asyncio.Task | None = None
        if self._task_store is not None:
            watcher = asyncio.create_task(self._task_watcher(), name="task-watcher")

        while not self._stop_event.is_set():
            try:
                await self._wait_for_activation()
                if self._stop_event.is_set():
                    break
                replace_autonomy_trigger(
                    getattr(self, "_continuation_trigger", None)
                    or AutonomyLevel.AUTONOMOUS
                )
                if self._rebalance_on_activation:
                    self._rebalance_on_activation = False
                    self._short_term.schedule_tree_rebalance_if_needed(
                        self._brain,
                        snapshot_path=self._snapshot_path,
                    )
                await self._cycle()
                self._consecutive_errors = 0  # 成功周期，重置错误计数
                if self._snapshot_path and not self.state.restart_requested:
                    self._short_term.active_provider = self._brain.current_provider_name
                    self._short_term.active_model = self._brain.current_model
                    self._short_term.save_to_file(self._snapshot_path)
            except RestartRequestedException:
                self.state.restart_requested = True
                break
            except asyncio.CancelledError:
                if self._stop_event.is_set():
                    break
                raise
            except AutonomyBlockedError as error:
                logger.info(tr("autonomy.cycle_paused", error=error))
            except Exception as e:
                self._consecutive_errors += 1
                logger.exception(
                    "Unexpected error in cycle "
                    f"({self._consecutive_errors}/{_MAX_CONSECUTIVE_ERRORS}): {e}"
                )

                if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    # 连续错误时先保存完整上下文，再清空短期记忆以重新开始。
                    backup_status = tr("loop.backup_empty")
                    if self._short_term.primary and self._snapshot_path:
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        backup_path = (
                            self._snapshot_path.parent / f"emergency_backup_{timestamp}.json"
                        )
                        try:
                            self._short_term.save_to_file(backup_path)
                            logger.warning(f"Emergency backup saved to {backup_path}")
                            backup_status = tr("loop.backup_created", path=backup_path)
                        except Exception as backup_err:
                            logger.error(f"Failed to save emergency backup: {backup_err}")
                            backup_status = tr(
                                "loop.backup_failed",
                                path=backup_path,
                                error=f"{type(backup_err).__name__}: {str(backup_err)[:200]}",
                            )
                    elif self._short_term.primary:
                        backup_status = tr("loop.backup_unconfigured")

                    self._short_term.primary.clear()
                    # 添加恢复通知
                    self._short_term.primary.append(
                        Message(
                            role="user",
                            source="system_recovery",
                            content=tr(
                                "loop.recovery",
                                count=self._consecutive_errors,
                                backup=backup_status,
                                error=f"{type(e).__name__}: {str(e)[:200]}",
                            ),
                        )
                    )
                    self._consecutive_errors = 0  # 重置计数器
                    await asyncio.sleep(_RECOVERY_COOLDOWN_SECONDS)
                else:
                    # 轻度错误处理：只添加简短错误信息，不重复累加
                    error_msg = tr(
                        "loop.system_error",
                        error=f"{type(e).__name__}: {str(e)[:200]}",
                    )
                    # 如果最后一条已经是同类错误，替换而不是追加，避免膨胀
                    if (
                        self._short_term.primary
                        and self._short_term.primary[-1].role == "user"
                        and self._short_term.primary[-1].source == "system_error"
                    ):
                        self._short_term.primary[-1] = Message(
                            role="user", content=error_msg, source="system_error"
                        )
                    else:
                        self._short_term.primary.append(
                            Message(role="user", content=error_msg, source="system_error")
                        )
                    # 使用指数退避，避免快速循环
                    wait_time = min(2 ** (self._consecutive_errors - 1), 60)
                    await asyncio.sleep(wait_time)

        if self._bubble_store is not None:
            self._bubble_store.cancel_all()

        if watcher is not None:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass

        self.state.is_running = False
        logger.info("AgentLoop stopped")

    def stop(self) -> None:
        self._stop_event.set()
        autonomy = getattr(self, "_autonomy", None)
        if autonomy is not None:
            autonomy.abort_waiters()
        # 唤醒 _rest() 中等待的消息事件，避免等满 idle_sleep_seconds 才退出
        self._inbox.message_event.set()

    async def wait_until_stopped(self) -> None:
        """Wait for an external shutdown/restart request without starting the loop."""

        await self._stop_event.wait()

    def request_restart(self, reason: str = "normal") -> None:
        """由受信任的管理入口请求安全重启。

        普通管理重启先保存结构完整的当前快照；首次初始化没有运行中的上下文，
        因此只记录重启意图并唤醒外层生命周期等待器。
        """
        if self._snapshot_path is not None and not self.state.setup_mode and reason != "bootstrap":
            self._short_term.active_provider = self._brain.current_provider_name
            self._short_term.active_model = self._brain.current_model
            self._short_term.save_to_file(self._snapshot_path)
        self.state.restart_requested = True
        self.state.restart_reason = reason
        self.stop()

    async def _cycle(self) -> None:
        if self.state.setup_mode:
            # Keep inbox messages queued while the first model connection is being
            # configured. Setup completion restarts into a fully initialized loop.
            await self._rest()
            return
        reinjected_pins = self._short_term.reinject_missing_pins()
        if reinjected_pins and self._ilog:
            self._ilog.log_pin_reinjected(
                [
                    {"pin_id": item.pin_id, "label": item.label, "content": item.content}
                    for item in reinjected_pins
                ]
            )
        max_batch = self._config.agent.inbox_batch_max
        known_event_ids = {
            event_id
            for message in self._short_term.primary
            for event_id in message.inbound_event_ids
        }
        events = await self._inbox.peek_claimable(
            lambda event: (
                event.event_id in known_event_ids
                or self._event_is_claimable(event)
            ),
            max_batch,
        )
        pending_known_triggers = [
            event.wake_level
            for event in self._inbox.pending_events()
            if event.event_id in known_event_ids and event.wake_level is not None
        ]
        event_triggers = [
            event.wake_level for event in events if event.wake_level is not None
        ] + pending_known_triggers
        if event_triggers:
            cycle_trigger = max(
                (
                    self._continuation_trigger or AutonomyLevel.SILENT,
                    *event_triggers,
                ),
                key=lambda level: level.rank,
            )
            self._continuation_trigger = cycle_trigger
            replace_autonomy_trigger(cycle_trigger)
        new_events = [
            event
            for event in events
            if event.event_id is None or event.event_id not in known_event_ids
        ]

        if new_events:
            content = self._build_content_blocks(new_events)
            self._short_term.primary.append(
                Message(
                    role="user",
                    content=content,
                    source=" + ".join(sorted({event.source for event in new_events})),
                    inbound_event_ids=[
                        event.event_id
                        for event in new_events
                        if event.event_id is not None
                    ],
                )
            )
            participants = {event.participant_id for event in new_events}
            if len(new_events) > 1:
                logger.info(
                    f"Processing {len(new_events)} batched messages from "
                    f"{len(participants)} participant(s): {participants}"
                )
            else:
                logger.info(f"Processing message from {new_events[0].participant_id}")
            if self._ilog:
                for e in new_events:
                    self._ilog.log_message_in(
                        e.participant_id,
                        e.content,
                        e.source,
                        e.attachments or None,
                        conversation_id=e.conversation_id,
                    )
            if self._snapshot_path:
                # The durable inbox remains unacknowledged until a model response
                # succeeds. Persist event IDs with the user message first so a
                # restart can retry without injecting the same content twice.
                self._short_term.save_to_file(self._snapshot_path)
            combined_text = " ".join(e.content for e in new_events if e.content)
            self.state.last_active = datetime.now()
            await self._auto_recall(combined_text)
            await self._task_reminder()
        elif not events:
            if self._subconscious is not None and self._short_term.should_compress():
                # 只把「即将被压缩掉的那段」交给潜意识，避免它反复提炼仍驻留的尾部内容。
                await self._subconscious.notify_pre_compress(self._short_term.compress_preview())
            _compress_system_prompt = self._prompt_builder.build()
            await self._short_term.compress_if_needed(
                self._brain,
                self._snapshot_path,
                agent_system_prompt=_compress_system_prompt,
            )
            await self._task_reminder()

        # 仅在「实际发生压缩」后刷新系统提示词：模型刚写入的 identity / thinking /
        # skills / palaces 内容仍在短期上下文里，等压缩导致上下文缓存失效时再统一进 prompt。
        if self._short_term.compress_generation != self._last_compress_generation:
            self._last_compress_generation = self._short_term.compress_generation
            if self._recent_activity is not None:
                self._recent_activity.schedule_sync_compressed_from_log(
                    self._short_term.raw_primary_boundary()
                )
            self._prompt_builder.refresh()
        system_prompt = self._prompt_builder.build()
        skill_load_warnings = self._prompt_builder.consume_skill_load_warnings()
        if skill_load_warnings:
            lines = [tr("loop.skill_warning_title")]
            lines.extend(f"- {warning}" for warning in skill_load_warnings)
            lines.append(tr("loop.skill_warning_tail"))
            self._short_term.primary.append(
                Message(role="user", content="\n".join(lines), source="skill_warning")
            )
            logger.warning(
                f"Injected {len(skill_load_warnings)} skill load warning(s) into model context"
            )
        if self._ilog:
            self._ilog.log_system_prompt(system_prompt)

        last_assistant = next(
            (m for m in reversed(self._short_term.primary) if m.role == "assistant"), None
        )
        if (
            self.state.tick
            and not events
            and not reinjected_pins
            and (not last_assistant or last_assistant.stop_reason != "tool_use")
            and self._short_term.primary
            and self._short_term.primary[-1].role != "user"
        ):
            tick_content = f"<{TICK_TAG}>"
            message = Message(role="user", content=tick_content, source="tick")
            self._short_term.primary.append(message)
            if self._ilog:
                self._ilog.log_message_tick(tick_content)

        if (
            self._brain.current_provider_name != self.state.current_provider
            or self._brain.current_model != self.state.current_model
        ):
            self.state.current_provider = self._brain.current_provider_name
            self.state.current_model = self._brain.current_model
            # 区分「失败降级」与「主动切换」：降级用更明确的措辞，提示模型这是被动容错。
            if self._brain.consume_fallback_switch():
                notice = tr(
                    "loop.model_fallback",
                    provider=self.state.current_provider,
                    model=self.state.current_model,
                )
            else:
                notice = tr(
                    "loop.model_switched",
                    provider=self.state.current_provider,
                    model=self.state.current_model,
                )
            self._short_term.primary.append(
                Message(role="user", content=notice, source="model_switch")
            )

        messages = self._short_term.build_context()
        if self._ilog:
            self._ilog.log_thinking_start(
                self.state.cycle_count,
                thinking=bool(self._brain.thinking),
            )
        response = await self._brain.think(
            messages,
            system_prompt,
            self._tools.get_schemas(model_has_vision=self._brain.current_model_has_vision),
            _autonomy_trigger=self._continuation_trigger or AutonomyLevel.AUTONOMOUS,
        )
        try:
            input_tokens = max(0, int(response.usage.get("input_tokens", 0) or 0))
        except (AttributeError, TypeError, ValueError):
            input_tokens = 0
        self.state.last_main_response_usage = (
            {
                "input_tokens": input_tokens,
                "provider": self._brain.current_provider_name,
                "model": response.model or self._brain.current_model,
                "measured_at": datetime.now().isoformat(),
            }
            if input_tokens
            else None
        )
        if self._ilog:
            self._ilog.log_llm_response(
                response.reasoning_content,
                response.content,
                response.tool_calls,
                response.stop_reason,
                response.model,
                response.usage,
                provider=self._brain.current_provider_name,
                thinking=bool(self._brain.thinking),
            )

        assistant_msg = Message(
            role="assistant",
            content=response.content,
            source=(
                f"{self._brain.current_provider_name}/{response.model or self._brain.current_model}"
            ),
            reasoning_content=response.reasoning_content,
            tool_calls=[
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in response.tool_calls
            ],
            stop_reason=response.stop_reason,
            usage=response.usage,
        )
        self._short_term.primary.append(assistant_msg)

        if response.tool_calls:
            # 执行工具前先保存快照，使崩溃恢复时能检测到待处理的 tool_call
            if self._snapshot_path and not self.state.restart_requested:
                self._short_term.save_to_file(self._snapshot_path)
            await self._act(response.tool_calls)
            recall_query = assistant_msg.reasoning_content or assistant_msg.content_text()
            await self._auto_recall(recall_query)
            await self._task_reminder()
        elif not events:
            self._continuation_trigger = None
            await self._rest()
        else:
            self._continuation_trigger = None

        if events:
            # An external admin restart may be requested while this cycle is in
            # flight.  Persist the completed assistant/tool chain before
            # acknowledging its durable events even in that case.  The
            # restart_self tool raises before reaching this block, so its
            # interrupted event remains unacknowledged for recovery.
            if self._snapshot_path:
                self._short_term.save_to_file(self._snapshot_path)
            await self._inbox.acknowledge(
                [event.event_id for event in events if event.event_id is not None]
            )

        self.state.cycle_count += 1
        self.state.current_provider = self._brain.current_provider_name
        self.state.current_model = self._brain.current_model

        if self._subconscious is not None:
            await self._subconscious.notify_cycle_complete(
                cycle_count=self.state.cycle_count,
                short_term_snapshot=list(self._short_term.primary),
                tool_calls_this_cycle=len(response.tool_calls),
            )

    @staticmethod
    def _build_content_blocks(events: list[IncomingEvent]) -> str | list[dict]:
        return build_content_blocks(events)

    async def _act(self, tool_calls) -> None:
        results: list[ToolResult] = []
        for tc in tool_calls:
            # ── 工具调用计数 ──
            self.state.tool_call_counts[tc.name] = self.state.tool_call_counts.get(tc.name, 0) + 1
            if self._ilog:
                self._ilog.log_tool_call(tc.id, tc.name, tc.arguments)
            if "__parse_error__" in tc.arguments:
                result = ToolResult(
                    tool_call_id=tc.id,
                    content=tr(
                        "loop.tool_parse_error",
                        error=tc.arguments["__parse_error__"],
                    ),
                    is_error=True,
                )
            else:
                result = await self._tools.execute(tc)
            result.tool_call_id = tc.id
            results.append(result)
            if self._ilog:
                content_str = (
                    result.content if isinstance(result.content, str) else str(result.content)
                )
                self._ilog.log_tool_result(tc.id, tc.name, content_str, result.is_error)
            if isinstance(result.content, str):
                logger.debug(f"Tool {tc.name}: {result.content[:80]}")

        for r in results:
            self._short_term.primary.append(
                Message(
                    role="tool",
                    content=r.content_blocks if r.content_blocks is not None else r.content,
                    tool_call_id=r.tool_call_id,
                    recalled_memory_ids=r.recalled_memory_ids,
                    source="tool",
                )
            )

    def _get_recalled_ids(self) -> set[str]:
        return {mid for m in self._short_term.primary for mid in m.recalled_memory_ids}

    async def _auto_recall(self, query_text: str) -> None:
        if not query_text.strip():
            return
        cfg = self._config.memory
        excluded = self._get_recalled_ids()
        if cfg.auto_recall_enabled and self._long_term._mem is not None:
            logger.debug("Starting long-term auto-recall")
            try:
                results = await self._long_term.query(query_text, limit=cfg.auto_recall_limit)
            except Exception:
                logger.debug("Long-term auto-recall query failed, skipping")
                results = []
            new = [
                m
                for m in results
                if m["id"] not in excluded and m["relevance"] >= cfg.auto_recall_relevance_threshold
            ]
            if new:
                lines = [tr("loop.auto_recall_title")]
                for i, m in enumerate(new, 1):
                    lines.append(
                        tr(
                            "loop.auto_recall_item",
                            index=i,
                            id=m["id"],
                            category=m["category"],
                            content=m["content"],
                            relevance=f"{m['relevance']:.2f}",
                        )
                    )
                self._short_term.primary.append(
                    Message(
                        role="user",
                        content="\n".join(lines),
                        recalled_memory_ids=[m["id"] for m in new],
                        source="auto_recall",
                    )
                )
                excluded.update(m["id"] for m in new)
                if self._ilog:
                    self._ilog.log_auto_recall(query_text, new)
                logger.debug(f"Auto-recalled {len(new)} long-term memories")

        if not getattr(cfg, "recent_activity_auto_recall_enabled", True):
            return
        if self._recent_activity is None:
            return
        try:
            recent = await self._recent_activity.query(
                query_text,
                limit=getattr(cfg, "recent_activity_auto_recall_limit", 2),
                min_relevance=getattr(cfg, "recent_activity_auto_recall_relevance_threshold", 0.72),
            )
        except Exception:
            logger.debug("Recent activity auto-recall query failed, skipping")
            return
        fresh = [m for m in recent if m.get("id") not in excluded]
        if not fresh:
            return
        replay = render_recent_activity_replay(
            fresh,
            title=tr("loop.recent_activity_title"),
            include_evidence=False,
        )
        self._short_term.primary.append(
            Message(
                role="user",
                content=replay,
                recalled_memory_ids=[m["id"] for m in fresh],
                source="recent_activity_auto_recall",
            )
        )
        logger.debug(f"Auto-recalled {len(fresh)} recent activities")

    async def _task_reminder(self) -> None:
        if self._task_store is None:
            return
        cycle_ok = (
            self.state.cycle_count - self._last_task_reminder_cycle >= self._task_reminder_interval
        )
        time_ok = time.monotonic() - self._last_task_reminder_time >= self._task_reminder_seconds
        if not (cycle_ok or time_ok):
            return
        self._last_task_reminder_cycle = self.state.cycle_count
        self._last_task_reminder_time = time.monotonic()
        purged = self._task_store.purge_completed()
        if purged:
            logger.debug(f"Auto-purged {purged} completed tasks")
        active = [t for t in self._task_store.list() if t.status in ("pending", "in_progress")]
        if not active:
            return
        lines = [tr("loop.task_reminder")]
        for t in active:
            suffix = tr("loop.task_details_hint") if t.details.strip() else ""
            lines.append(
                f"- [{t.id}] [{t.status}] [{format_task_times(t)}] {t.description}{suffix} "
            )
        self._short_term.primary.append(
            Message(role="user", content="\n".join(lines), source="task_reminder")
        )
        if self._ilog:
            self._ilog.log_task_reminder(
                [
                    {
                        "id": t.id,
                        "status": t.status,
                        "description": t.description,
                        "has_details": bool(t.details.strip()),
                        "created_at": t.created_at,
                        "updated_at": t.updated_at,
                    }
                    for t in active
                ],
                source="cycle",
            )
        logger.debug(f"Task reminder injected: {len(active)} active tasks")

    async def _task_watcher(self) -> None:
        task_store = self._task_store
        if task_store is None:
            return
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._task_reminder_seconds)
                break
            except TimeoutError:
                pass
            if not self.state.is_sleeping:
                continue
            if self._inbox.has_source("task_reminder"):
                continue
            active = [t for t in task_store.list() if t.status in ("pending", "in_progress")]
            if not active:
                continue
            lines = [tr("loop.task_wakeup")]
            for t in active:
                suffix = tr("loop.task_details_hint") if t.details.strip() else ""
                lines.append(
                    f"- [{t.id}] [{t.status}] {t.description}{suffix} {format_task_times(t)}"
                )
            await self._inbox.push(
                IncomingEvent(
                    participant_id="system",
                    content="\n".join(lines),
                    source="task_reminder",
                    timestamp=datetime.now(),
                    wake_level=AutonomyLevel.EVENT_DRIVEN,
                )
            )
            if self._ilog:
                self._ilog.log_task_reminder(
                    [
                        {
                            "id": t.id,
                            "status": t.status,
                            "description": t.description,
                            "has_details": bool(t.details.strip()),
                            "created_at": t.created_at,
                            "updated_at": t.updated_at,
                        }
                        for t in active
                    ],
                    source="sleep_interrupt",
                )
            logger.debug(f"Task watcher interrupted sleep: {len(active)} active tasks")

    async def _rest(self) -> None:
        self.state.is_sleeping = True
        try:
            autonomy_event = self._autonomy.change_event
            level = self._autonomy.level
            if level is not AutonomyLevel.AUTONOMOUS:
                logger.info(tr("autonomy.waiting"))
                await self._wait_for_runtime_signal(
                    timeout=None,
                    autonomy_event=autonomy_event,
                )
                logger.info(tr("autonomy.rest_ended"))
            else:
                timeout = self._config.agent.idle_sleep_seconds
                logger.info(f"Agent entering rest for {timeout}s")
                try:
                    await self._wait_for_runtime_signal(
                        timeout=timeout,
                        autonomy_event=autonomy_event,
                    )
                except TimeoutError:
                    logger.info(f"Agent rest timed out after {timeout}s")
                else:
                    logger.info(tr("autonomy.rest_ended"))
        finally:
            self.state.is_sleeping = False

    def _event_is_claimable(self, event: IncomingEvent) -> bool:
        return event.wake_level is None or self._autonomy.allows(
            AutonomyScope.MAIN,
            trigger=event.wake_level,
        )

    async def _wakeable_trigger(self) -> AutonomyLevel | None:
        if not any(
            self._autonomy.allows(AutonomyScope.MAIN, trigger=wake_level)
            for wake_level in self._inbox.pending_wake_levels()
        ):
            return None
        events = await self._inbox.peek_claimable(
            self._event_is_claimable,
            self._config.agent.inbox_batch_max,
        )
        candidates = [
            event.wake_level for event in events if event.wake_level is not None
        ]
        return max(candidates, key=lambda level: level.rank) if candidates else None

    async def _wait_for_activation(self) -> None:
        while not self._stop_event.is_set():
            self._inbox.acknowledge_non_wakeable()
            autonomy_event = self._autonomy.change_event
            if (
                self._continuation_trigger is not None
                and self._autonomy.allows(
                    AutonomyScope.MAIN,
                    trigger=self._continuation_trigger,
                )
            ):
                return
            wake_trigger = await self._wakeable_trigger()
            if wake_trigger is not None:
                self._continuation_trigger = wake_trigger
                return
            if self._autonomy.allows(
                AutonomyScope.MAIN,
                trigger=AutonomyLevel.AUTONOMOUS,
            ):
                self._continuation_trigger = AutonomyLevel.AUTONOMOUS
                return
            self.state.is_sleeping = True
            try:
                await self._wait_for_runtime_signal(
                    timeout=None,
                    autonomy_event=autonomy_event,
                )
            finally:
                self.state.is_sleeping = False

    async def _wait_for_runtime_signal(
        self,
        timeout: float | None,
        *,
        autonomy_event: asyncio.Event | None = None,
    ) -> None:
        waiters = {
            asyncio.create_task(self._inbox.message_event.wait()),
            asyncio.create_task((autonomy_event or self._autonomy.change_event).wait()),
            asyncio.create_task(self._stop_event.wait()),
        }
        try:
            done, pending = await asyncio.wait(
                waiters,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError
            for task in done:
                task.result()
        finally:
            for task in waiters:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
