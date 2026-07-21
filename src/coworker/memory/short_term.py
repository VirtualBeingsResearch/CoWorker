from __future__ import annotations

import asyncio
import bisect
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

from coworker.core.types import (
    ConversationThread,
    Message,
    PinnedItem,
    SummaryResult,
    estimate_content_tokens,
)
from coworker.i18n import bind_locale, tr
from coworker.memory.memory_tree import MemoryBlockTree, MemoryNode

if TYPE_CHECKING:
    from coworker.agent.log_store import LogStore
    from coworker.brain.brain import Brain

TokenCountSource = Literal["estimated", "exact"]


class ShortTermMemory:
    def __init__(
        self,
        max_tokens: int = 80_000,
        compress_threshold: float = 0.55,
        compress_ratio: float = 0.25,
        compress_protected_tail: float = 0.40,
        log_store: LogStore | None = None,
        tree_enabled: bool = True,
        tree_tail_fraction: float = 0.60,
        tree_spine_cap_fraction: float = 0.40,
        tree_backfill_concurrency: int = 5,
        tree_merge_reach_depth: int = 2,
    ) -> None:
        self.primary: list[Message] = []
        self.pinned_items: list[PinnedItem] = []
        self.threads: dict[str, ConversationThread] = {}
        self.active_provider: str = ""
        self.active_model: str = ""
        self._max_tokens = max_tokens
        self._compress_threshold = compress_threshold
        self._compress_ratio = compress_ratio
        self._compress_protected_tail = compress_protected_tail
        self._compressing = False
        self._compress_task: asyncio.Task | None = None
        self._tree_rebalance_task: asyncio.Task | None = None
        # 每发生一次实际压缩自增，作为进程内「上下文已被压缩」的信号，
        # 供主循环检测并在压缩时刷新系统提示词快照。
        self.compress_generation: int = 0

        # 多分辨率记忆块树。tree_enabled 时取代「单锚点压缩」：被压缩的尾部切片
        # 提升为树叶并按时间尺度级联合并；脊柱活在 primary 之外，由 build_context 渲染。
        self._tree_enabled = tree_enabled
        self._log_store = log_store
        # tree_enabled 时尾部保护比例改由 tree_tail_fraction 决定（取代 legacy compress_protected_tail）
        self._protected_tail_fraction = (
            tree_tail_fraction if tree_enabled else compress_protected_tail
        )
        self.tree = MemoryBlockTree(
            spine_cap_tokens=int(max_tokens * tree_spine_cap_fraction),
            reach_depth=tree_merge_reach_depth,
        )
        self._compress_lock = asyncio.Lock()
        self._backfill_concurrency = max(1, tree_backfill_concurrency)
        # 历史回溯进度（供 API 轮询）：{running, done, total}。回溯是长操作，需要可观测。
        self.backfill_progress: dict = {"running": False, "done": 0, "total": 0}

    @property
    def log_store(self) -> LogStore | None:
        return self._log_store

    def get_thread(self, participant_id: str) -> ConversationThread:
        if participant_id not in self.threads:
            self.threads[participant_id] = ConversationThread(participant_id=participant_id)
        return self.threads[participant_id]

    def pin(self, pin_id: str, label: str, content: str, file_path: str | None = None) -> None:
        existing = next((item for item in self.pinned_items if item.pin_id == pin_id), None)
        if existing is not None:
            # primary 中已可见的 pin 消息不能原地改写，否则会破坏 provider 前缀缓存一致性。
            existing.label = label
            existing.content = content
            existing.file_path = file_path
        else:
            # 新 pin 只写入 pinned_items，不立即插入 primary，
            # 避免在 tool_use→tool_result 之间注入 user 消息破坏对话结构。
            # reinject_missing_pins() 会在下一个 cycle 开头将其补入。
            self.pinned_items.append(
                PinnedItem(pin_id=pin_id, label=label, content=content, file_path=file_path)
            )

    def unpin(self, pin_id: str) -> bool:
        before = len(self.pinned_items)
        self.pinned_items = [item for item in self.pinned_items if item.pin_id != pin_id]
        # Do not rewrite already model-visible messages: changing primary in place
        # invalidates provider prefix caches. Unpin only stops future reinjection;
        # any visible pin message leaves naturally via compression/clear.
        return len(self.pinned_items) < before

    def list_pinned(self) -> list[PinnedItem]:
        return list(self.pinned_items)

    def pinned_as_messages(self) -> list[Message]:
        """Return pinned items as Message objects, refreshing file-backed content."""
        result = []
        for item in self.pinned_items:
            content = self._load_pin_content(item)
            result.append(
                Message(
                    role="user",
                    content=f"[{item.label}]\n{content}",
                    pin_id=item.pin_id,
                    source="pinned_context",
                )
            )
        return result

    def clear(self) -> int:
        """清空主消息列表，保留 pinned items，返回被清除的消息数量。

        末尾的 assistant[tool_use] 消息会被保留，确保调用方写入的 tool_result
        有合法的父消息，不会产生孤立的 tool result 导致 API 报错。
        """
        # 如果最后一条是带 tool_calls 的 assistant 消息，说明当前正处于工具执行中，保留它
        tail: list[Message] = []
        if self.primary and self.primary[-1].role == "assistant" and self.primary[-1].tool_calls:
            tail = [self.primary[-1]]
        count = len(self.primary) - len(tail)
        self.primary = tail
        return count

    def reinject_missing_pins(self) -> list[PinnedItem]:
        if not self.pinned_items:
            return []
        current_pin_ids = {m.pin_id for m in self.primary if m.pin_id}
        reinjected: list[PinnedItem] = []
        for item in self.pinned_items:
            if item.pin_id not in current_pin_ids:
                content = self._load_pin_content(item)
                self.primary.append(
                    Message(
                        role="user",
                        content=f"[{item.label}]\n{content}",
                        pin_id=item.pin_id,
                        source="pinned_context",
                    )
                )
                reinjected.append(item)
        if reinjected:
            logger.debug(
                f"Reinjected {len(reinjected)} pinned message(s): {[item.pin_id for item in reinjected]}"
            )
        return reinjected

    def _load_pin_content(self, item: PinnedItem) -> str:
        if item.file_path is None:
            return item.content
        try:
            from pathlib import Path

            new_content = Path(item.file_path).read_text(encoding="utf-8")
            item.content = new_content
            return new_content
        except Exception as e:
            logger.warning(
                f"Pin '{item.pin_id}': failed to re-read {item.file_path}: {e}, using cached content"
            )
            return item.content

    def build_context(self) -> list[Message]:
        if self._tree_enabled and self.tree.nodes:
            # 脊柱（多尺度时间摘要）作为只读前缀注入，随后接原始尾部消息。
            return self.tree.render() + list(self.primary)
        return list(self.primary)

    def _select_promotion_slice(self, total_tokens: int) -> tuple[list[Message], int]:
        """The oldest slice that the next compression/promotion would consume.

        Shared by ``compress_preview`` and ``_do_compress_inner`` so the slice fed to
        the subconscious (→ mem0) equals the slice actually promoted into the tree.
        """
        cutoff = self._compress_cutoff(total_tokens)
        return list(self.primary[:cutoff]), cutoff

    def _select_full_promotion_slice(self) -> tuple[list[Message], int]:
        """Return the full live prefix that manual compression may promote.

        If compression is invoked as a tool, the current assistant[tool_use] message
        is already the last primary message. Keep it in primary so the upcoming
        tool_result still has a valid parent.
        """
        cutoff = len(self.primary)
        if self.primary and self.primary[-1].role == "assistant" and self.primary[-1].tool_calls:
            cutoff -= 1
        return list(self.primary[:cutoff]), cutoff

    def _promoted_slice_intact(self, to_compress: list[Message], cutoff: int) -> bool:
        """压缩跑在后台任务里，`brain.summarize` 的 await 期间主循环可能改动 primary 前缀
        （clear / unpin / 重启恢复截断 / cleanup_incomplete_tool_calls）。splice 用的是
        索引，若前缀已变，`primary[:cutoff]=...` 会误删/错配。故按对象身份核验：只有当
        primary 头部仍是我们摘要过的那批对象时才允许 splice，否则放弃本次压缩。"""
        return len(self.primary) >= cutoff and all(
            self.primary[i] is to_compress[i] for i in range(len(to_compress))
        )

    def estimate_tokens(self, brain: Brain | None = None) -> int:
        if brain is not None and brain.active_provider is not None:
            provider = brain.active_provider
            model_id = brain.current_model
            return sum(provider.estimate_content_tokens(m.content, model_id) for m in self.primary)
        return sum(estimate_content_tokens(m.content) for m in self.primary)

    async def _do_compress(
        self,
        brain: Brain,
        context_hint: str | None = None,
        agent_system_prompt: str = "",
    ) -> tuple[int, int]:
        """Execute compression. Returns (messages_compressed, memories_saved).

        memories_saved is always 0: the compressor only produces the summary anchor;
        long-term extraction is owned by the subconscious (pre-compress + periodic).
        """
        context_hint = context_hint or tr("memory.autonomous_context")
        if self._compressing or len(self.primary) < 4:
            return 0, 0
        # 关键：_compressing 必须在任何 await 之前同步置位，否则「检查→获取锁」之间的窗口
        # 会让第二个调用者也通过 if 检查，被锁串行后跑出第二次压缩。锁只负责串行临界区
        # （选片→摘要→splice→树变更），不充当重入门闩——门闩是这个同步置位的标志。
        self._compressing = True
        try:
            async with self._compress_lock:
                return await self._do_compress_inner(brain, context_hint, agent_system_prompt)
        finally:
            self._compressing = False

    def _compress_cutoff(self, total_tokens: int) -> int:
        """Index such that ``primary[:cutoff]`` is what compression would compress.

        The newest ``compress_protected_tail`` share of tokens is never compressed;
        within the remainder, walk forward up to ``compress_ratio`` of tokens, then
        extend to keep trailing tool-call chains intact. Returns 0 if there is nothing
        worth compressing (fewer than 2 messages would be cut).
        """
        # Determine protected tail boundary (newest N% of tokens are never compressed)
        protected_budget = int(total_tokens * self._protected_tail_fraction)
        protected_start = len(self.primary)
        tail_tokens = 0
        for i in range(len(self.primary) - 1, -1, -1):
            tail_tokens += estimate_content_tokens(self.primary[i].content)
            if tail_tokens >= protected_budget:
                protected_start = i + 1
                break

        # Walk forward to find cutoff within compress_ratio token budget
        compress_budget = int(total_tokens * self._compress_ratio)
        cutoff = 0
        accumulated = 0
        for i in range(protected_start):
            t = estimate_content_tokens(self.primary[i].content)
            if accumulated + t > compress_budget:
                break
            accumulated += t
            cutoff = i + 1

        if cutoff < 2:
            return 0

        # Extend cutoff over the FULL trailing tool-result run — even past protected_start.
        # 否则当一组并行 tool_result 跨越 protected_start 边界时，会把 assistant[tool_use]
        # 压进切片、却把它的部分 tool_result 留在保留侧开头，形成「孤儿 tool_result」
        # （无父 tool_use），下一次 build_context 会被 provider 拒绝。宁可多压一点也要保链完整。
        n = len(self.primary)
        while cutoff < n and self.primary[cutoff].role == "tool":
            cutoff += 1
        return cutoff

    def compress_preview(self) -> list[Message]:
        """Best-effort preview of the messages the next compression would compress.

        Uses the heuristic token estimate (no brain), so it may differ slightly from
        the real cutoff, but it lets callers (e.g. the pre-compress subconscious) act
        on just the soon-to-be-lost slice instead of the whole context.
        """
        slice_, _cutoff = self._select_promotion_slice(self.estimate_tokens())
        return slice_

    def compress_all_preview(self) -> list[Message]:
        """Preview the exact live prefix that ``compress_all_now`` would compress."""
        slice_, _cutoff = self._select_full_promotion_slice()
        return slice_

    @staticmethod
    def _is_legacy_summary_anchor(message: Message) -> bool:
        return message.role == "user" and (
            message.source == "memory_summary"
            or (
                isinstance(message.content, str)
                and message.content.startswith("[记忆：以下是我之前的行动摘要]")
            )
        )

    def raw_primary_boundary(self) -> datetime | None:
        """Oldest timestamp still present as raw short-term context.

        Recent-activity indexing uses this as the loss boundary: interaction-log
        events older than this timestamp have been compressed out of ``primary`` and
        are no longer available verbatim in short-term memory. Legacy single-anchor
        summaries are skipped because they are compressed replacements, not raw
        retained context.
        """
        raw = [m.timestamp for m in self.primary if not self._is_legacy_summary_anchor(m)]
        if raw:
            return min(raw)
        if self.tree.nodes or any(self._is_legacy_summary_anchor(m) for m in self.primary):
            return datetime.now()
        return None

    @staticmethod
    def _parse_summary(raw: str | SummaryResult) -> str:
        if isinstance(raw, SummaryResult):
            raw = raw.content
        # JSONDecodeError: 非 JSON；AttributeError: JSON 是 list/str 等非 dict，无 .get。
        try:
            return str(json.loads(raw).get("summary", raw))
        except (json.JSONDecodeError, AttributeError) as e:
            logger.debug(f"Summary parsing failed, using raw text. Raw summary: {raw!r} Error: {e}")
            return raw

    @staticmethod
    def _summary_text_tokens_and_source(
        raw: str | SummaryResult,
    ) -> tuple[str, int, TokenCountSource]:
        if isinstance(raw, SummaryResult):
            summary = ShortTermMemory._parse_summary(raw.content)
            tokens = raw.output_tokens
            if tokens > 0:
                return summary, tokens, "exact"
            return summary, estimate_content_tokens(summary), "estimated"
        summary = ShortTermMemory._parse_summary(raw)
        return summary, estimate_content_tokens(summary), "estimated"

    def _make_summarize_fn(
        self,
        brain: Brain,
        agent_system_prompt: str = "",
    ) -> Callable[[str, str], Awaitable[str | SummaryResult]]:
        """Adapt brain.summarize into the (text, hint)->summary callable the tree expects."""

        async def _fn(text: str, hint: str) -> str | SummaryResult:
            raw = await brain.summarize(
                [Message(role="user", content=text)],
                context_hint=hint,
                agent_system_prompt=agent_system_prompt,
                return_usage=True,
                # No stm_context for tree merges — input is already a summary, self-contained.
            )
            if isinstance(raw, SummaryResult):
                return SummaryResult(
                    content=self._parse_summary(raw.content),
                    usage=raw.usage,
                )
            return self._parse_summary(raw)

        return _fn

    @staticmethod
    def _tree_summary_target_tokens(budget: int) -> int:
        return max(8, int(budget * 0.80))

    @staticmethod
    def _tree_leaf_context_hint(context_hint: str, budget: int) -> str:
        target = ShortTermMemory._tree_summary_target_tokens(budget)
        return tr(
            "memory.leaf_hint",
            context=context_hint,
            target=target,
            budget=budget,
        )

    @staticmethod
    def _tree_retry_context_hint(
        context_hint: str,
        last_tokens: int,
        budget: int,
        *,
        empty: bool = False,
    ) -> str:
        if empty:
            return tr(
                "memory.retry_empty",
                context=context_hint,
                target=ShortTermMemory._tree_summary_target_tokens(budget),
                budget=budget,
            )
        return tr(
            "memory.retry_over",
            context=context_hint,
            last_tokens=last_tokens,
            budget=budget,
            target=ShortTermMemory._tree_summary_target_tokens(budget),
        )

    async def _summarize_messages_to_tree_budget(
        self,
        brain: Brain,
        messages: list[Message],
        context_hint: str,
        agent_system_prompt: str = "",
        stm_context: list[Message] | None = None,
    ) -> tuple[str, int, TokenCountSource]:
        budget = self.tree.node_budget()
        hint = self._tree_leaf_context_hint(context_hint, budget)
        last = ""
        last_tokens = 0
        last_source: TokenCountSource = "estimated"
        for _ in range(3):
            raw = await brain.summarize(
                messages,
                context_hint=hint,
                agent_system_prompt=agent_system_prompt,
                stm_context=stm_context,
                return_usage=True,
            )
            last, last_tokens, last_source = self._summary_text_tokens_and_source(raw)
            if last.strip() and last_tokens <= budget:
                return last, last_tokens, last_source
            hint = self._tree_retry_context_hint(
                context_hint,
                last_tokens,
                budget,
                empty=not last.strip(),
            )
        logger.warning(
            f"Memory tree leaf summary remained over budget after retries: {last_tokens}>{budget} tokens"
        )
        if last.strip():
            return last, last_tokens, last_source
        fallback = tr("memory.empty_messages")
        return fallback, estimate_content_tokens(fallback), "estimated"

    @staticmethod
    async def _summarize_text_to_tree_budget(
        summarize: Callable[[str, str], Awaitable[str | SummaryResult]],
        text: str,
        context_hint: str,
        budget: int,
    ) -> tuple[str, int, TokenCountSource]:
        hint = context_hint
        last = ""
        last_tokens = 0
        last_source: TokenCountSource = "estimated"
        for _ in range(3):
            raw = await summarize(text, hint)
            last, last_tokens, last_source = ShortTermMemory._summary_text_tokens_and_source(raw)
            if last.strip() and last_tokens <= budget:
                return last, last_tokens, last_source
            hint = ShortTermMemory._tree_retry_context_hint(
                context_hint,
                last_tokens,
                budget,
                empty=not last.strip(),
            )
        logger.warning(
            f"Memory tree text summary remained over budget after retries: {last_tokens}>{budget} tokens"
        )
        if last.strip():
            return last, last_tokens, last_source
        fallback = tr("memory.empty_logs")
        return fallback, estimate_content_tokens(fallback), "estimated"

    async def _do_compress_inner(
        self,
        brain: Brain,
        context_hint: str,
        agent_system_prompt: str = "",
    ) -> tuple[int, int]:
        if self._tree_enabled:
            return await self._compress_into_tree(brain, context_hint, agent_system_prompt)

        # ---- legacy 单锚点路径（tree_enabled=False 回退）----
        total_tokens = self.estimate_tokens(brain)
        to_compress, cutoff = self._select_promotion_slice(total_tokens)
        if cutoff < 2:
            return 0, 0

        logger.info(f"Compressing {len(to_compress)} messages")
        raw = await brain.summarize(
            to_compress,
            context_hint=context_hint,
            agent_system_prompt=agent_system_prompt,
        )
        summary_text = self._parse_summary(raw)

        if not self._promoted_slice_intact(to_compress, cutoff):
            logger.warning("Compress aborted: primary prefix changed during summarize")
            return 0, 0
        self.primary[:cutoff] = [
            Message(
                role="user",
                content=tr("memory.legacy_anchor", summary=summary_text),
                source="memory_summary",
            )
        ]
        self.compress_generation += 1
        logger.info(f"Compressed {len(to_compress)} messages to summary anchor")
        return len(to_compress), 0

    async def _compress_into_tree(
        self,
        brain: Brain,
        context_hint: str,
        agent_system_prompt: str = "",
    ) -> tuple[int, int]:
        """Promote the oldest tail slice into the memory block tree (multi-resolution).

        The leaf is summarized from the LIVE slice (in-memory, no log read). Coarser
        levels (created on merge) re-summarize from stored child summaries (no raw log
        read), reaching down for finer material per ``reach_depth``. The promoted slice
        leaves ``primary`` entirely; the spine is rendered as a prefix in ``build_context``.
        """
        total_tokens = self.estimate_tokens(brain)
        to_compress, cutoff = self._select_promotion_slice(total_tokens)
        if cutoff < 2:
            return 0, 0

        logger.info(f"Promoting {len(to_compress)} messages into memory tree")
        stm_context = self.tree.render() if agent_system_prompt else None
        (
            leaf_summary,
            leaf_tokens,
            leaf_token_source,
        ) = await self._summarize_messages_to_tree_budget(
            brain,
            to_compress,
            context_hint=context_hint,
            agent_system_prompt=agent_system_prompt,
            stm_context=stm_context,
        )
        # 摘要 await 期间主循环可能改动 primary 前缀；前缀已变则放弃，避免误删尾部消息。
        if not self._promoted_slice_intact(to_compress, cutoff):
            logger.warning("Promotion aborted: primary prefix changed during summarize")
            return 0, 0
        # primary 也按插入序而非时间戳保序，切片首/尾消息的时间戳未必是最早/最晚。
        # 取 min/max 包络（与回溯 _chunk_span 一致），避免叶子时间窗反转、span 反转。
        stamps = [m.timestamp for m in to_compress]
        leaf = MemoryNode(
            level=0,
            summary=leaf_summary,
            t_start=min(stamps),
            t_end=max(stamps),
            msg_count=len(to_compress),
            token_estimate=leaf_tokens,
            token_count_source=leaf_token_source,
        )
        # temp tree 上跑 fib_carry：不触碰 self.tree，避免产生中间态。
        # 节点浅拷贝安全：fib_carry 只新建 merged 节点，不修改已有节点对象。
        temp_tree = self.tree.clone_empty()
        temp_tree.nodes.extend(self.tree.nodes)
        summarize = self._make_summarize_fn(brain, agent_system_prompt)
        await temp_tree.promote_leaf(leaf, summarize=summarize)

        # fib_carry 期间 primary 也可能被外部改动，再检查一次。
        if not self._promoted_slice_intact(to_compress, cutoff):
            logger.warning("Promotion aborted: primary prefix changed during fib_carry")
            return 0, 0

        # 原子替换：三步均为同步操作，asyncio 协作式调度保证无事件循环插入点。
        # _compress_lock 已在 _do_compress 层持有，与 backfill_tree_online 互斥。
        self.tree = temp_tree
        self.primary[:cutoff] = []
        self.compress_generation += 1
        logger.info(
            f"Promoted {len(to_compress)} messages; tree now {len(self.tree.nodes)} node(s), "
            f"~{self.tree.spine_tokens()} spine tokens"
        )
        return len(to_compress), 0

    async def _do_compress_all(
        self,
        brain: Brain,
        context_hint: str | None = None,
        agent_system_prompt: str = "",
    ) -> tuple[int, int]:
        """Compress every currently live primary message except an active tool_use tail."""
        context_hint = context_hint or tr("memory.manual_context")
        if self._compressing:
            return 0, 0
        self._compressing = True
        try:
            async with self._compress_lock:
                return await self._do_compress_all_inner(brain, context_hint, agent_system_prompt)
        finally:
            self._compressing = False

    async def _do_compress_all_inner(
        self,
        brain: Brain,
        context_hint: str,
        agent_system_prompt: str = "",
    ) -> tuple[int, int]:
        to_compress, cutoff = self._select_full_promotion_slice()
        if not to_compress:
            return 0, 0
        if self._tree_enabled:
            return await self._compress_full_slice_into_tree(
                brain, to_compress, cutoff, context_hint, agent_system_prompt
            )

        logger.info(f"Compressing all {len(to_compress)} live messages to summary anchor")
        raw = await brain.summarize(
            to_compress,
            context_hint=context_hint,
            agent_system_prompt=agent_system_prompt,
        )
        summary_text = self._parse_summary(raw)

        if not self._promoted_slice_intact(to_compress, cutoff):
            logger.warning("Full compression aborted: primary prefix changed during summarize")
            return 0, 0
        self.primary[:cutoff] = [
            Message(
                role="user",
                content=tr("memory.legacy_anchor", summary=summary_text),
                source="memory_summary",
            )
        ]
        self.compress_generation += 1
        logger.info(f"Compressed all {len(to_compress)} live messages to summary anchor")
        return len(to_compress), 0

    async def _compress_full_slice_into_tree(
        self,
        brain: Brain,
        to_compress: list[Message],
        cutoff: int,
        context_hint: str,
        agent_system_prompt: str = "",
    ) -> tuple[int, int]:
        logger.info(f"Promoting all {len(to_compress)} live messages into memory tree")
        stm_context = self.tree.render() if agent_system_prompt else None
        (
            leaf_summary,
            leaf_tokens,
            leaf_token_source,
        ) = await self._summarize_messages_to_tree_budget(
            brain,
            to_compress,
            context_hint=context_hint,
            agent_system_prompt=agent_system_prompt,
            stm_context=stm_context,
        )
        if not self._promoted_slice_intact(to_compress, cutoff):
            logger.warning("Full promotion aborted: primary prefix changed during summarize")
            return 0, 0

        stamps = [m.timestamp for m in to_compress]
        leaf = MemoryNode(
            level=0,
            summary=leaf_summary,
            t_start=min(stamps),
            t_end=max(stamps),
            msg_count=len(to_compress),
            token_estimate=leaf_tokens,
            token_count_source=leaf_token_source,
        )
        temp_tree = self.tree.clone_empty()
        temp_tree.nodes.extend(self.tree.nodes)
        summarize = self._make_summarize_fn(brain, agent_system_prompt)
        await temp_tree.promote_leaf(leaf, summarize=summarize)

        if not self._promoted_slice_intact(to_compress, cutoff):
            logger.warning("Full promotion aborted: primary prefix changed during fib_carry")
            return 0, 0

        self.tree = temp_tree
        self.primary[:cutoff] = []
        self.compress_generation += 1
        logger.info(
            f"Promoted all {len(to_compress)} live messages; tree now {len(self.tree.nodes)} "
            f"node(s), ~{self.tree.spine_tokens()} spine tokens"
        )
        return len(to_compress), 0

    def should_compress(self) -> bool:
        if self._compressing:
            return False
        threshold = int(self._max_tokens * self._compress_threshold)
        return self.estimate_tokens() >= threshold

    async def compress_if_needed(
        self,
        brain: Brain,
        snapshot_path: Path | None = None,
        agent_system_prompt: str = "",
    ) -> None:
        if self._compressing:
            return
        threshold = int(self._max_tokens * self._compress_threshold)
        token_count = await brain.count_tokens(self.primary)
        if token_count < threshold:
            return
        self._compress_task = asyncio.create_task(
            bind_locale(
                lambda: self._do_compress_and_snapshot(brain, snapshot_path, agent_system_prompt)
            )
        )
        self._compress_task.add_done_callback(self._on_compress_done)

    async def _do_compress_and_snapshot(
        self,
        brain: Brain,
        snapshot_path: Path | None,
        agent_system_prompt: str = "",
    ) -> tuple[int, int]:
        result = await self._do_compress(brain, agent_system_prompt=agent_system_prompt)
        if snapshot_path:
            self.save_to_file(snapshot_path)
            logger.debug("Snapshot saved after background compression")
        return result

    @staticmethod
    def _on_compress_done(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            logger.error(f"Background compression failed: {task.exception()}")

    def schedule_tree_rebalance_if_needed(
        self,
        brain: Brain,
        snapshot_path: Path | None = None,
        agent_system_prompt: str = "",
    ) -> bool:
        """后台调度预算迁移，避免启动路径被旧树重压摘要卡住。"""
        if not self._tree_enabled or not self.tree.needs_rebalance():
            return False
        if self._tree_rebalance_task is not None and not self._tree_rebalance_task.done():
            return False

        self._tree_rebalance_task = asyncio.create_task(
            bind_locale(
                lambda: self._rebalance_tree_and_snapshot(brain, snapshot_path, agent_system_prompt)
            ),
            name="memory-tree-rebalance",
        )
        self._tree_rebalance_task.add_done_callback(self._on_tree_rebalance_done)
        logger.info("Scheduled memory tree budget rebalance in background")
        return True

    async def _rebalance_tree_and_snapshot(
        self,
        brain: Brain,
        snapshot_path: Path | None,
        agent_system_prompt: str = "",
    ) -> bool:
        changed = await self.rebalance_tree_if_needed(brain, agent_system_prompt)
        if changed and snapshot_path:
            self.save_to_file(snapshot_path)
            logger.debug("Snapshot saved after background memory-tree rebalance")
        return changed

    @staticmethod
    def _on_tree_rebalance_done(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            logger.error(f"Background memory-tree rebalance failed: {task.exception()}")

    async def compress_now(
        self,
        brain: Brain,
        context_hint: str | None = None,
        agent_system_prompt: str = "",
    ) -> tuple[int, int]:
        """Force compress regardless of threshold. Returns (messages_compressed, memories_saved)."""
        return await self._do_compress(brain, context_hint, agent_system_prompt)

    async def compress_all_now(
        self,
        brain: Brain,
        context_hint: str | None = None,
        agent_system_prompt: str = "",
    ) -> tuple[int, int]:
        """Force-compress all live primary messages while preserving an active tool_use tail."""
        return await self._do_compress_all(brain, context_hint, agent_system_prompt)

    @staticmethod
    def _chunk_span(chunk: list[dict[str, Any]]) -> tuple[datetime, datetime]:
        """块的时间范围 = 块内**可解析** ts 的 min/max，对坏行稳健（避免单条坏 ts 把叶子
        标签拉到未来）。全部不可解析时退回 now（极端边角）。"""
        parsed = []
        for e in chunk:
            try:
                parsed.append(datetime.fromisoformat(e["ts"]))
            except (KeyError, ValueError, TypeError):
                continue
        if not parsed:
            now = datetime.now()
            return now, now
        return min(parsed), max(parsed)

    def _backfill_before(self) -> datetime | None:
        """回溯的时间上界 = primary 最旧消息时间，避免脊柱与仍在 primary 的近期内容重叠。"""
        return self.primary[0].timestamp if self.primary else None

    async def _populate_tree(
        self,
        tree: MemoryBlockTree,
        brain: Brain,
        max_leaves: int,
        before: datetime | None,
    ) -> int:
        """把（before 之前的）日志全史分块、**并行**摘要成 L0 叶子，再用**平衡两两归约**建成
        给定 ``tree`` 的脊柱。返回叶子数。

        不触碰 self.tree——离线传 self.tree（已 reset），在线传一棵临时树。

        用平衡归约而非流式 promote_leaf：后者对 2^k 个时间密集（无缝）叶子会塌成单节点；
        平衡归约产出均匀的多节点脊柱。叶子并行摘要 + 归约层并行 + 合并走「摘要叠摘要」
        （不重读日志），三管齐下提速。叶子仍从原始 chunk 摘要（保真），原文随时可下钻。

        全程更新 self.backfill_progress；try/finally 保证 running 标志最终复位。
        """
        self.backfill_progress = {"running": True, "done": 0, "total": 0}
        try:
            if self._log_store is None:
                return 0
            chunks = self._log_store.backfill_chunks(before=before, max_chunks=max_leaves)
            if not chunks:
                logger.info("[backfill] 无可回溯的历史内容")
                return 0
            total = len(chunks)
            self.backfill_progress["total"] = total
            logger.info(f"[backfill] 开始：{total} 块，并行摘要为叶子…")
            summarize = self._make_summarize_fn(brain)

            # 1) 并行把每块摘要成 L0 叶子（有界并发；进度随完成数递增）
            sem = asyncio.Semaphore(self._backfill_concurrency)
            done_lock = asyncio.Lock()  # 保护 backfill_progress 的 done 计数，避免竞争条件

            async def _make_leaf(chunk: list[dict[str, Any]]) -> MemoryNode | None:
                digest = self._log_store.digest_entries(chunk) if self._log_store else None
                node: MemoryNode | None = None
                if digest:
                    async with sem:
                        budget = tree.node_budget()
                        (
                            summary,
                            summary_tokens,
                            summary_token_source,
                        ) = await self._summarize_text_to_tree_budget(
                            summarize,
                            digest,
                            tr(
                                "memory.backfill_hint",
                                target=self._tree_summary_target_tokens(budget),
                                budget=budget,
                            ),
                            budget,
                        )
                    t_start, t_end = self._chunk_span(chunk)
                    node = MemoryNode(
                        level=0,
                        summary=summary,
                        t_start=t_start,
                        t_end=t_end,
                        msg_count=len(chunk),
                        token_estimate=summary_tokens,
                        token_count_source=summary_token_source,
                    )
                async with done_lock:
                    self.backfill_progress["done"] = self.backfill_progress.get("done", 0) + 1
                return node

            # gather 保持输入顺序 → 叶子按时序排列（平衡归约配对相邻=配对相邻时间）
            results = await asyncio.gather(*(_make_leaf(c) for c in chunks))
            leaves = [n for n in results if n is not None]
            logger.info(f"[backfill] {len(leaves)} 叶子完成，平衡归约建脊柱…")

            # 2) 平衡两两归约（绝不塌成单节点；归约合并走 summary-of-summaries，不重读日志）
            await tree.build_balanced(leaves, summarize, concurrency=self._backfill_concurrency)
            return len(leaves)
        finally:
            self.backfill_progress["running"] = False

    async def backfill_tree_from_log(self, brain: Brain, max_leaves: int = 64) -> int:
        """【离线】一次性从原始日志全史**就地重建**记忆树（覆盖现有树）。返回叶子数。

        仅供进程未在跑主循环时使用（如 `--backfill-tree` CLI）；在线运行请用
        ``backfill_tree_online``，否则会与主循环争用 self.tree。
        """
        if not self._tree_enabled or self._log_store is None:
            return 0
        before = self._backfill_before()
        self.tree.reset()
        built = await self._populate_tree(self.tree, brain, max_leaves, before)
        self.compress_generation += 1
        logger.info(f"Backfilled {built} leaves from log; spine now {len(self.tree.nodes)} node(s)")
        return built

    async def backfill_tree_online(self, brain: Brain, max_leaves: int = 64) -> int:
        """【在线】运行中安全回溯：建一棵**临时树**（全程不碰 self.tree），建完在压缩锁内
        **原子替换**。替换时保留构建期间主线压缩新提升的（晚于 before 的）节点，避免覆盖丢失。

        返回回溯叶子数；若无可回溯内容（n==0）则不替换、保留活树原样。
        """
        if not self._tree_enabled or self._log_store is None:
            return 0
        before = self._backfill_before()
        temp = self.tree.clone_empty()
        built = await self._populate_tree(temp, brain, max_leaves, before)
        if built == 0:
            return 0
        async with self._compress_lock:
            # 构建是漫长的 await 过程，期间主线可能压缩并向 self.tree 提升了晚于 before 的新节点；
            # 这些节点 temp 不含（被 before 截断），原子替换前把它们接到 temp 末尾，避免丢失。
            # 用 t_end（而非 t_start）判定：主线合并可能把一个 pre-before 老节点与刚提升的
            # post-before 新节点并成一个 t_start<before 的节点；按 t_start 会误删它、丢失新内容。
            # 按 t_end>=before 保留任何「覆盖到 before 之后内容」的节点（边界节点轻微重叠 << 静默丢失）。
            if before is not None:
                temp.nodes.extend(n for n in self.tree.nodes if n.t_end >= before)
            self.tree = temp
            self.compress_generation += 1
        logger.info(f"Online-backfilled {built} leaves; spine now {len(self.tree.nodes)} node(s)")
        return built

    async def rebalance_tree_if_needed(
        self,
        brain: Brain,
        agent_system_prompt: str = "",
    ) -> bool:
        """当前配置预算变小时，按新 K 重塑已恢复的记忆树。

        快照只持久化节点，不持久化旧预算；进程以新配置构造 MemoryBlockTree 后再 load
        旧节点，因此这里用当前 tree 配置检查形态。若旧节点来自更大的预算，可能有 level
        超过当前 K，必须在启动后、brain 可用时补一次进位合并。
        """
        summarize = self._make_summarize_fn(brain, agent_system_prompt)

        for attempt in range(2):
            async with self._compress_lock:
                if not self._tree_enabled or not self.tree.needs_rebalance():
                    return False
                generation = self.compress_generation
                temp = self.tree.clone_empty()
                temp.nodes = [n.clone() for n in self.tree.nodes]

            changed = await temp.rebalance_for_budget(summarize)
            if not changed:
                return False

            async with self._compress_lock:
                if self.compress_generation != generation:
                    logger.info(
                        "Memory tree changed during budget rebalance; "
                        f"retrying on latest tree ({attempt + 1}/2)"
                    )
                    continue

                self.tree = temp
                self.compress_generation += 1
                logger.info(
                    f"Rebalanced memory tree for current budget: {len(self.tree.nodes)} node(s), "
                    f"~{self.tree.spine_tokens()} spine tokens"
                )
                return True

        logger.warning("Skipped memory tree budget rebalance because the tree changed repeatedly")
        return False

    def _heal_legacy_tree_nodes(self) -> int:
        """修复旧版（含 bug）快照里的树节点。幂等；无 log_store / 无需修复时为 no-op。

        旧 ``_merge`` 有两处缺陷会污染持久化的节点，升级后这些坏节点不会自愈，且 raw_available
        =False 的子节点还会经 ``children_have_raw`` 闸门把「不可重摘要」传染给其父节点：
          ① span 反转：旧实现取 ``[a.t_start, b.t_end]``，时间戳乱序时 ``t_start>b.t_end`` →
             规范化为 ``[min, max]``。
          ② raw_available 误判：旧实现只要本次没走重摘要（如回溯 ``build_balanced`` 刻意传
             ``rederive=None``）就置 False，可原始日志其实仍在盘上。这里回探日志：若该区间确有
             可下钻记录则翻回 True（仅一次全量扫描，结果随下次快照持久化、此后即 no-op）。
        返回被修复的节点数。"""
        nodes = self.tree.nodes
        if not nodes:
            return 0
        healed = 0
        for n in nodes:
            if n.t_start > n.t_end:
                n.t_start, n.t_end = n.t_end, n.t_start
                healed += 1
        stale = [n for n in nodes if not n.raw_available]
        if stale and self._log_store is not None:
            entries, complete = self._log_store.read_all()
            if complete and entries:
                # read_all 已按 (ts, seq) 升序：用 ts 串做 bisect 切窗，digest_entries 复用
                # 同一套 _DIGEST_TYPES 过滤，区间内有可下钻内容才判定原始可达。
                stamps = [str(e.get("ts", "")) for e in entries]
                for n in stale:
                    lo = bisect.bisect_left(stamps, n.t_start.isoformat())
                    hi = bisect.bisect_right(stamps, n.t_end.isoformat())
                    if hi > lo and self._log_store.digest_entries(entries[lo:hi]):
                        n.raw_available = True
                        healed += 1
        if healed:
            logger.info(f"Healed {healed} legacy memory-tree node fixup(s) on load")
        return healed

    def serialize(self) -> dict:
        return {
            "primary": [
                {
                    **m.to_dict(),
                    "timestamp": m.timestamp.isoformat(),
                    "source": m.source,
                    **({"usage": m.usage} if m.usage else {}),
                }
                for m in self.primary
            ],
            "pinned_items": [item.to_dict() for item in self.pinned_items],
            "threads": {pid: t.to_dict() for pid, t in self.threads.items()},
            "active_provider": self.active_provider,
            "active_model": self.active_model,
            "tree": self.tree.serialize(),
        }

    @staticmethod
    def parse_primary(data: dict) -> list[Message]:
        """从快照/备份 dict 解析 primary 消息列表（不构造整个 STM）。供 deserialize 与备份恢复复用。"""
        out: list[Message] = []
        for m in data.get("primary", []):
            msg = Message(
                role=m["role"],
                content=m["content"],
                tool_calls=m.get("tool_calls", []),
                tool_call_id=m.get("tool_call_id"),
                recalled_memory_ids=m.get("recalled_memory_ids", []),
                pin_id=m.get("pin_id"),
                source=m.get("source"),
                usage=m.get("usage", {}),
            )
            if "timestamp" in m:
                msg.timestamp = datetime.fromisoformat(m["timestamp"])
            out.append(msg)
        return out

    @classmethod
    def deserialize(cls, data: dict, **kwargs) -> ShortTermMemory:
        # kwargs 透传给 __init__（max_tokens / compress_* / log_store / tree_* 等）。
        mem = cls(**kwargs)
        mem.tree.load(data.get("tree", {}))
        mem._heal_legacy_tree_nodes()
        mem.primary.extend(cls.parse_primary(data))
        for p in data.get("pinned_items", []):
            try:
                mem.pinned_items.append(PinnedItem.from_dict(p))
            except Exception as e:
                logger.warning(f"Failed to restore pinned item: {e}")
        for pid, t in data.get("threads", {}).items():
            mem.threads[pid] = ConversationThread.from_dict(t)
        mem.active_provider = data.get("active_provider", "")
        mem.active_model = data.get("active_model", "")
        return mem

    def save_to_file(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.serialize(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load_from_file(cls, path: Path, **kwargs) -> ShortTermMemory:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.deserialize(data, **kwargs)

    def cleanup_incomplete_tool_calls(self) -> int:
        """Remove trailing incomplete tool call chains. Returns number of messages removed."""
        if not self.primary:
            return 0
        for i in range(len(self.primary) - 1, -1, -1):
            msg = self.primary[i]
            if msg.role == "assistant" and msg.tool_calls:
                expected_ids = {tc["id"] for tc in msg.tool_calls}
                returned_ids = {
                    m.tool_call_id
                    for m in self.primary[i + 1 :]
                    if m.role == "tool" and m.tool_call_id
                }
                if not expected_ids.issubset(returned_ids):
                    removed = len(self.primary) - i
                    self.primary = self.primary[:i]
                    return removed
            elif msg.role in ("user", "system"):
                break
        return 0
