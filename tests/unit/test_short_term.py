from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from coworker.core.types import Message
from coworker.memory.short_term import ShortTermMemory


class TestShortTermMemory:
    def test_get_thread_creates_on_first_access(self):
        mem = ShortTermMemory()
        thread = mem.get_thread("alice")
        assert thread.participant_id == "alice"
        assert thread is mem.get_thread("alice")  # same object

    def test_get_thread_isolates_participants(self):
        mem = ShortTermMemory()
        alice = mem.get_thread("alice")
        bob = mem.get_thread("bob")
        alice.add(Message(role="user", content="alice msg"))
        assert len(bob.messages) == 0

    def test_build_context_returns_primary(self):
        mem = ShortTermMemory()
        mem.primary.append(Message(role="assistant", content="thinking"))
        ctx = mem.build_context()
        assert len(ctx) == 1
        assert ctx[0].content == "thinking"

    def test_build_context_empty_primary(self):
        mem = ShortTermMemory()
        ctx = mem.build_context()
        assert ctx == []

    @pytest.mark.parametrize(
        "anchor_text",
        [
            "[记忆：以下是我之前的行动摘要]\nold",
            "[memory: summary of my previous actions]\nold",
        ],
    )
    def test_legacy_summary_anchor_is_normalized_while_loading(self, anchor_text):
        raw_timestamp = datetime(2026, 7, 8, 9)
        mem = ShortTermMemory.deserialize(
            {
                "primary": [
                    {
                        "role": "user",
                        "content": anchor_text,
                        "timestamp": datetime(2026, 7, 8, 12).isoformat(),
                    },
                    {
                        "role": "assistant",
                        "content": "still raw",
                        "timestamp": raw_timestamp.isoformat(),
                    },
                ]
            },
            tree_enabled=False,
        )

        assert mem.primary[0].source == "memory_summary"
        assert mem.raw_primary_boundary() == raw_timestamp

    def test_raw_primary_boundary_none_when_no_compressed_context(self):
        mem = ShortTermMemory()
        assert mem.raw_primary_boundary() is None

    def test_estimate_tokens(self):
        from coworker.core.types import estimate_text_tokens
        mem = ShortTermMemory()
        content = "a" * 400
        mem.primary.append(Message(role="assistant", content=content))
        assert mem.estimate_tokens() == estimate_text_tokens(content)

    def test_serialize_deserialize_round_trip(self):
        mem = ShortTermMemory(max_tokens=1000, compress_threshold=0.5)
        mem.primary.append(Message(role="assistant", content="thought"))
        thread = mem.get_thread("alice")
        thread.add(Message(role="user", content="hi alice"))

        data = mem.serialize()
        restored = ShortTermMemory.deserialize(data, max_tokens=1000, compress_threshold=0.5)

        assert len(restored.primary) == 1
        assert restored.primary[0].content == "thought"
        assert "alice" in restored.threads
        assert restored.threads["alice"].messages[0].content == "hi alice"

    def test_save_and_load_file(self, tmp_path):
        mem = ShortTermMemory()
        mem.primary.append(Message(role="assistant", content="persisted"))
        state_file = tmp_path / "state.json"
        mem.save_to_file(state_file)

        loaded = ShortTermMemory.load_from_file(state_file)
        assert loaded.primary[0].content == "persisted"

    @pytest.mark.asyncio
    async def test_compress_if_needed_below_threshold(self, mock_provider):
        from coworker.brain.brain import Brain
        brain = Brain("mock", "mock-model")
        brain.register_provider(mock_provider)

        mem = ShortTermMemory(max_tokens=10_000, compress_threshold=0.8)
        mem.primary.append(Message(role="assistant", content="short"))

        original_len = len(mem.primary)
        await mem.compress_if_needed(brain)
        assert len(mem.primary) == original_len  # nothing changed
        assert mem.compress_generation == 0  # no compression → counter unchanged

    @pytest.mark.asyncio
    async def test_compress_if_needed_above_threshold(self, mock_long_term):
        from coworker.brain.brain import Brain
        from coworker.core.types import LLMResponse
        from tests.conftest import MockProvider

        provider = MockProvider(LLMResponse(
            content="compressed summary。关键词：test",
            tool_calls=[], stop_reason="end_turn",
            model="mock-model", usage={},
        ))
        brain = Brain("mock", "mock-model")
        brain.register_provider(provider)

        # set very low threshold so a few messages trigger compression
        # tree_enabled=False: exercise the legacy single-anchor path explicitly.
        mem = ShortTermMemory(max_tokens=10, compress_threshold=0.1, tree_enabled=False)
        for i in range(8):
            mem.primary.append(Message(role="user", content=f"message {i} " * 5))

        await mem.compress_if_needed(brain)
        # compression runs in background; wait for it
        assert mem._compress_task is not None
        await mem._compress_task

        # primary should be shorter now
        assert len(mem.primary) < 8
        # summary message injected
        assert any("行动摘要" in m.content for m in mem.primary)
        # an actual compression bumps the generation counter
        assert mem.compress_generation == 1
        # compressor no longer writes long-term memory (owned by the subconscious)
        mock_long_term.write.assert_not_awaited()

    def test_compress_preview_empty_when_too_small(self):
        mem = ShortTermMemory(max_tokens=10_000)
        mem.primary.append(Message(role="user", content="hi"))
        mem.primary.append(Message(role="assistant", content="hello"))
        assert mem.compress_preview() == []

    def test_compress_all_preview_excludes_active_tool_use_tail(self):
        mem = ShortTermMemory()
        for i in range(2):
            mem.primary.append(Message(role="user", content=f"msg {i}"))
        tail = Message(
            role="assistant",
            content="",
            tool_calls=[{"id": "tc1", "name": "clear_short_term_memory", "arguments": {}}],
        )
        mem.primary.append(tail)

        preview = mem.compress_all_preview()

        assert preview == mem.primary[:2]
        assert tail not in preview

    @pytest.mark.asyncio
    async def test_compress_preview_matches_compressed_slice(self):
        from coworker.brain.brain import Brain
        from coworker.core.types import LLMResponse
        from tests.conftest import MockProvider

        provider = MockProvider(LLMResponse(
            content="s",
            tool_calls=[], stop_reason="end_turn", model="mock-model", usage={},
        ))
        brain = Brain("mock", "mock-model")
        brain.register_provider(provider)

        mem = ShortTermMemory(max_tokens=10, compress_threshold=0.1, tree_enabled=False)
        for i in range(8):
            mem.primary.append(Message(role="user", content=f"message {i} " * 5))

        preview = mem.compress_preview()
        assert len(preview) >= 2
        # preview is a prefix that excludes the protected (newest) tail
        assert preview == mem.primary[:len(preview)]
        assert preview[-1] is not mem.primary[-1]
        preview_contents = [m.content for m in preview]

        # The previewed slice is exactly what compression replaces with a summary.
        await mem.compress_now(brain)
        assert mem.primary[0].content.startswith("[记忆：以下是我之前的行动摘要")
        assert len(mem.primary) == 8 - len(preview) + 1
        remaining = [m.content for m in mem.primary[1:]]
        assert all(c not in remaining for c in preview_contents)

    @staticmethod
    def _summary_brain():
        from coworker.brain.brain import Brain
        from coworker.core.types import LLMResponse
        from tests.conftest import MockProvider

        provider = MockProvider(LLMResponse(
            content="节点摘要",
            tool_calls=[], stop_reason="end_turn", model="mock-model", usage={},
        ))
        brain = Brain("mock", "mock-model")
        brain.register_provider(provider)
        return brain

    @pytest.mark.asyncio
    async def test_tree_promotion_moves_slice_out_and_renders_prefix(self):
        brain = self._summary_brain()
        mem = ShortTermMemory(max_tokens=10, compress_threshold=0.1)  # tree_enabled default True
        for i in range(8):
            mem.primary.append(Message(role="user", content=f"message {i} " * 5))

        promoted, _ = await mem.compress_now(brain)
        assert promoted >= 2
        # The promoted slice is gone from primary (no anchor message left behind)
        assert len(mem.primary) == 8 - promoted
        assert not any("压缩摘要" in str(m.content) for m in mem.primary)
        # A spine node now exists and build_context renders it as a system prefix
        assert len(mem.tree.nodes) >= 1
        ctx = mem.build_context()
        assert ctx[0].role == "system"
        assert "[记忆 " in ctx[0].content
        assert ctx[len(mem.tree.nodes):] == mem.primary

    @pytest.mark.asyncio
    async def test_tree_promotion_uses_reported_summary_output_tokens(self):
        from coworker.brain.brain import Brain
        from coworker.core.types import LLMResponse, estimate_content_tokens
        from tests.conftest import MockProvider

        provider = MockProvider(LLMResponse(
            content="节点摘要",
            tool_calls=[], stop_reason="end_turn",
            model="mock-model", usage={"input_tokens": 100, "output_tokens": 37},
        ))
        brain = Brain("mock", "mock-model")
        brain.register_provider(provider)

        mem = ShortTermMemory(max_tokens=10, compress_threshold=0.1)
        for i in range(8):
            mem.primary.append(Message(role="user", content=f"message {i} " * 5))

        await mem.compress_now(brain)

        assert mem.tree.nodes[0].summary == "节点摘要"
        assert estimate_content_tokens("节点摘要") != 37
        assert mem.tree.nodes[0].token_estimate == 37
        assert mem.tree.nodes[0].token_count_source == "exact"
        assert mem.serialize()["tree"]["nodes"][0]["token_count_source"] == "exact"

    @pytest.mark.asyncio
    async def test_tree_serialize_round_trip(self):
        brain = self._summary_brain()
        mem = ShortTermMemory(max_tokens=10, compress_threshold=0.1)
        for i in range(8):
            mem.primary.append(Message(role="user", content=f"message {i} " * 5))
        await mem.compress_now(brain)
        assert len(mem.tree.nodes) >= 1

        data = mem.serialize()
        assert "tree" in data
        restored = ShortTermMemory.deserialize(data)
        assert len(restored.tree.nodes) == len(mem.tree.nodes)
        assert restored.tree.nodes[0].summary == mem.tree.nodes[0].summary
        assert restored.tree.nodes[0].t_start == mem.tree.nodes[0].t_start

    @pytest.mark.asyncio
    async def test_restored_tree_rebalances_for_current_budget(self):
        # 模拟旧快照来自更大的记忆树预算；用较小预算配置恢复后，应在 brain 可用时重塑。
        from datetime import datetime, timedelta

        from coworker.memory.memory_tree import MemoryBlockTree, MemoryNode

        async def summarize(text: str, hint: str) -> str:
            return "节点摘要"

        base = datetime(2026, 6, 1, 9, 0, 0)
        high = MemoryBlockTree(spine_cap_tokens=16_000, leaf_budget_tokens=600)
        for i in range(60):
            t = base + timedelta(minutes=i)
            await high.promote_leaf(
                MemoryNode(level=0, summary="叶", t_start=t, t_end=t, msg_count=1),
                summarize,
            )

        restored = ShortTermMemory.deserialize(
            {"tree": high.serialize(), "primary": [], "threads": {}},
            max_tokens=3000,
            tree_spine_cap_fraction=0.4,
        )
        assert restored.tree.needs_rebalance()

        changed = await restored.rebalance_tree_if_needed(self._summary_brain())

        assert changed is True
        assert restored.compress_generation == 1
        assert not restored.tree.needs_rebalance()

    @pytest.mark.asyncio
    async def test_schedule_tree_rebalance_runs_in_background(self, tmp_path):
        # 启动恢复只调度迁移，不等待慢摘要；任务完成后再原子替换并保存快照。
        from datetime import datetime, timedelta

        from coworker.memory.memory_tree import MemoryBlockTree, MemoryNode

        async def summarize(text: str, hint: str) -> str:
            return "节点摘要"

        base = datetime(2026, 6, 1, 9, 0, 0)
        high = MemoryBlockTree(spine_cap_tokens=16_000, leaf_budget_tokens=600)
        for i in range(60):
            t = base + timedelta(minutes=i)
            await high.promote_leaf(
                MemoryNode(level=0, summary="叶", t_start=t, t_end=t, msg_count=1),
                summarize,
            )

        restored = ShortTermMemory.deserialize(
            {"tree": high.serialize(), "primary": [], "threads": {}},
            max_tokens=3000,
            tree_spine_cap_fraction=0.4,
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_summarize(messages, context_hint="", **_):
            started.set()
            await release.wait()
            return "节点摘要"

        brain = MagicMock()
        brain.summarize = AsyncMock(side_effect=slow_summarize)
        snapshot_path = tmp_path / "short_term_snapshot.json"

        scheduled = restored.schedule_tree_rebalance_if_needed(brain, snapshot_path=snapshot_path)

        assert scheduled is True
        assert restored._tree_rebalance_task is not None
        await asyncio.wait_for(started.wait(), timeout=1)
        assert restored.compress_generation == 0
        assert restored.tree.needs_rebalance()

        release.set()
        changed = await restored._tree_rebalance_task

        assert changed is True
        assert restored.compress_generation == 1
        assert not restored.tree.needs_rebalance()
        assert snapshot_path.exists()

    @pytest.mark.asyncio
    async def test_compress_bails_if_prefix_mutated_during_summarize(self):
        # C1: brain.summarize 的 await 期间主循环 clear() 了 primary 前缀 →
        # 压缩必须放弃 splice，不能误删新消息、也不能加叶子。
        brain = self._summary_brain()
        mem = ShortTermMemory(max_tokens=10, compress_threshold=0.1)
        for i in range(8):
            mem.primary.append(Message(role="user", content=f"message {i} " * 5))

        async def evil_summarize(messages, context_hint="", **_):
            mem.primary.clear()  # 模拟并发 clear()
            mem.primary.append(Message(role="user", content="brand new"))
            return "s"

        brain.summarize = evil_summarize  # type: ignore[method-assign]
        promoted, _ = await mem.compress_now(brain)
        assert promoted == 0  # 放弃
        assert len(mem.tree.nodes) == 0  # 未加叶子
        assert [m.content for m in mem.primary] == ["brand new"]  # 新消息完好未被误删

    @pytest.mark.asyncio
    async def test_compress_bails_if_prefix_mutated_during_fib_carry(self):
        # C2: fib_carry 的 merge-summarize await 期间主循环改动 primary 前缀 →
        # 第二次 _promoted_slice_intact 检查必须捕获并放弃，self.tree 不应被替换。
        from datetime import datetime

        from coworker.memory.memory_tree import MemoryNode

        brain = self._summary_brain()
        mem = ShortTermMemory(max_tokens=10, compress_threshold=0.1)
        for i in range(8):
            mem.primary.append(Message(role="user", content=f"message {i} " * 5))

        # 预先塞 1 个 level-0 节点：添加新叶后共 2 个 > quota(1) → fib_carry 触发 merge。
        ts = datetime(2024, 1, 1)
        mem.tree.nodes.append(MemoryNode(level=0, summary="old", t_start=ts, t_end=ts, msg_count=5))
        original_tree_id = id(mem.tree)

        async def evil_merge(text: str, hint: str) -> str:
            mem.primary.clear()
            mem.primary.append(Message(role="user", content="mutated during fib_carry"))
            return "merged"

        mem._make_summarize_fn = lambda b, asp="": evil_merge  # type: ignore[method-assign]

        promoted, _ = await mem.compress_now(brain)
        assert promoted == 0  # 放弃
        assert id(mem.tree) == original_tree_id  # tree 对象未被替换
        assert len(mem.tree.nodes) == 1  # 仍是旧树（1 个节点）
        assert [m.content for m in mem.primary] == ["mutated during fib_carry"]
        assert mem.compress_generation == 0  # 未触发刷新

    @pytest.mark.asyncio
    async def test_tree_primary_generation_updated_atomically(self):
        # C3: fib_carry 期间 self.tree / primary / compress_generation 均未变化；
        # 三者在 fib_carry 完成后的原子替换步骤里同时更新。
        from datetime import datetime

        from coworker.memory.memory_tree import MemoryNode

        brain = self._summary_brain()
        mem = ShortTermMemory(max_tokens=10, compress_threshold=0.1)
        for i in range(8):
            mem.primary.append(Message(role="user", content=f"message {i} " * 5))

        ts = datetime(2024, 1, 1)
        mem.tree.nodes.append(MemoryNode(level=0, summary="old", t_start=ts, t_end=ts, msg_count=5))
        original_tree_id = id(mem.tree)
        original_primary_len = len(mem.primary)

        state: dict = {}

        async def observing_merge(text: str, hint: str) -> str:
            # fib_carry 期间：三者均未更新
            state["tree_id"] = id(mem.tree)
            state["primary_len"] = len(mem.primary)
            state["compress_gen"] = mem.compress_generation
            return "merged"

        mem._make_summarize_fn = lambda b, asp="": observing_merge  # type: ignore[method-assign]

        promoted, _ = await mem.compress_now(brain)
        assert promoted >= 2

        # fib_carry 期间：tree 未替换、primary 未截、generation 未增
        assert state["tree_id"] == original_tree_id
        assert state["primary_len"] == original_primary_len
        assert state["compress_gen"] == 0

        # 原子替换后：三者同时更新
        assert id(mem.tree) != original_tree_id
        assert len(mem.primary) == original_primary_len - promoted
        assert mem.compress_generation == 1

    @pytest.mark.asyncio
    async def test_compress_tree_passes_agent_system_prompt_and_stm_context(self):
        # Verify that agent_system_prompt and tree.render() are forwarded to brain.summarize.
        from unittest.mock import AsyncMock, MagicMock
        brain = MagicMock()
        brain.summarize = AsyncMock(return_value="叶子摘要")
        brain.active_provider = None

        mem = ShortTermMemory(max_tokens=10, compress_threshold=0.1)
        for i in range(8):
            mem.primary.append(Message(role="user", content=f"msg {i} " * 5))

        await mem.compress_now(brain, agent_system_prompt="agent身份提示")

        assert brain.summarize.called
        call_kwargs = brain.summarize.call_args_list[0]
        assert call_kwargs.kwargs.get("agent_system_prompt") == "agent身份提示"
        context_hint = call_kwargs.kwargs.get("context_hint", "")
        assert "tokens" in context_hint
        assert "连续记忆概要" in context_hint
        assert "硬上限" in context_hint
        assert "数量/编号降噪" in context_hint
        assert "多个/一批/多轮/若干" in context_hint
        assert "只保留未闭环、阻塞、验收依据" in context_hint
        assert "下一步接续点" in context_hint
        assert "接续：" in context_hint
        assert "不要堆砌普通关键词" in context_hint
        assert "不要输出“关键词：”列表" in context_hint
        # stm_context should be the tree's render output (empty list for first compression)
        assert "stm_context" in call_kwargs.kwargs

    @pytest.mark.asyncio
    async def test_compress_tree_retries_over_budget_leaf_summary(self):
        from unittest.mock import AsyncMock, MagicMock
        brain = MagicMock()
        brain.summarize = AsyncMock(side_effect=["巨" * 1000, "短"])
        brain.active_provider = None

        mem = ShortTermMemory(max_tokens=10, compress_threshold=0.1)
        for i in range(8):
            mem.primary.append(Message(role="user", content=f"msg {i} " * 5))

        await mem.compress_now(brain)

        assert brain.summarize.await_count == 2
        retry_hint = brain.summarize.call_args_list[1].kwargs.get("context_hint", "")
        assert "上一版摘要约" in retry_hint
        assert "请重新压缩到" in retry_hint
        assert "目标约" in retry_hint
        assert "下一步接续点" in retry_hint
        assert "关键词：" in retry_hint
        assert "默认概括为多个/一批/多轮/若干" in retry_hint
        assert "才保留具体编号" in retry_hint
        assert "最后一句必须是接续状态" in retry_hint
        assert "不要解释" in retry_hint

    @pytest.mark.asyncio
    async def test_compress_tree_retries_empty_leaf_summary(self):
        from unittest.mock import AsyncMock, MagicMock
        brain = MagicMock()
        brain.summarize = AsyncMock(side_effect=["", "短"])
        brain.active_provider = None

        mem = ShortTermMemory(max_tokens=10, compress_threshold=0.1)
        for i in range(8):
            mem.primary.append(Message(role="user", content=f"msg {i} " * 5))

        await mem.compress_now(brain)

        assert brain.summarize.await_count == 2
        retry_hint = brain.summarize.call_args_list[1].kwargs.get("context_hint", "")
        assert "上一版摘要为空或不可用" in retry_hint
        assert "不要输出空白" in retry_hint
        assert mem.tree.nodes[0].summary == "短"

    @pytest.mark.asyncio
    async def test_compress_tree_no_agent_system_prompt_uses_objective(self):
        # Without agent_system_prompt, brain.summarize should be called without it (objective mode).
        from unittest.mock import AsyncMock, MagicMock
        brain = MagicMock()
        brain.summarize = AsyncMock(return_value="摘要")
        brain.active_provider = None

        mem = ShortTermMemory(max_tokens=10, compress_threshold=0.1)
        for i in range(8):
            mem.primary.append(Message(role="user", content=f"msg {i} " * 5))

        await mem.compress_now(brain)

        assert brain.summarize.called
        call_kwargs = brain.summarize.call_args
        assert call_kwargs.kwargs.get("agent_system_prompt", "") == ""

    def test_cutoff_pulls_full_tool_chain_no_orphan(self):
        # H2: assistant[tool_use] 后跟一长串 tool_result 跨越保护边界时，
        # cutoff 必须吃下整条链，保留侧不得以孤儿 tool_result 开头。
        mem = ShortTermMemory(max_tokens=100, tree_enabled=True)
        mem.primary.append(Message(role="assistant", content="call",
                                   tool_calls=[{"id": "x", "type": "function",
                                                "function": {"name": "f", "arguments": "{}"}}]))
        for _ in range(7):
            mem.primary.append(Message(role="tool", content="r", tool_call_id="x"))
        mem.primary.append(Message(role="user", content="后续"))
        mem.primary.append(Message(role="user", content="再后续"))

        cutoff = mem._compress_cutoff(mem.estimate_tokens())
        kept = mem.primary[cutoff:]
        assert not kept or kept[0].role != "tool"  # 保留侧不以孤儿 tool 开头

    @staticmethod
    def _write_log(tmp_path, n, base, body_chars=300):
        from datetime import timedelta

        from coworker.agent.log_store import LogStore
        body = "项目部署与回滚的讨论。" * max(1, body_chars // 11)
        log = tmp_path / "interactions.jsonl"
        log.write_text("\n".join(json.dumps({
            "type": "message_in", "participant_id": "u", "seq": i,
            "content": f"历史{i}：{body}",
            "ts": (base + timedelta(minutes=i)).isoformat(),
        }, ensure_ascii=False) for i in range(n)) + "\n", encoding="utf-8")
        return LogStore(tmp_path)

    @pytest.mark.asyncio
    async def test_backfill_builds_spine_from_log(self, tmp_path):
        from datetime import datetime
        store = self._write_log(tmp_path, 40, datetime(2026, 6, 1, 9, 0, 0))  # ~12k 字符 → 多叶子
        brain = self._summary_brain()
        mem = ShortTermMemory(max_tokens=2000, log_store=store)
        n = await mem.backfill_tree_from_log(brain, max_leaves=8)
        assert n >= 2  # 足够内容 → 多叶子
        assert len(mem.tree.nodes) >= 1  # 级联合并后可能 < 叶子数
        assert mem.build_context()[0].role == "system"  # 脊柱前缀

    @pytest.mark.asyncio
    async def test_backfill_dense_log_does_not_collapse(self, tmp_path):
        from datetime import datetime
        # 回归：时间密集（逐分钟、同日、无时间缝）且块数接近 2 的幂时，
        # 旧的流式级联会塌成单节点（popcount）；平衡归约必须保留多节点。
        store = self._write_log(tmp_path, 64, datetime(2026, 6, 1, 9, 0, 0), body_chars=500)
        brain = self._summary_brain()
        mem = ShortTermMemory(max_tokens=80000, log_store=store)
        n = await mem.backfill_tree_from_log(brain, max_leaves=64)
        assert n >= 4
        assert len(mem.tree.nodes) > 1  # 不塌成 1 个

    @pytest.mark.asyncio
    async def test_backfill_respects_max_leaves(self, tmp_path):
        from datetime import datetime
        # ~30k 字符：不设上限会切出 ~7 块，max_leaves=4 必须把它压回 ≤5
        store = self._write_log(tmp_path, 60, datetime(2026, 6, 1, 9, 0, 0), body_chars=500)
        brain = self._summary_brain()
        mem = ShortTermMemory(max_tokens=2000, log_store=store)
        n = await mem.backfill_tree_from_log(brain, max_leaves=4)
        assert 1 < n <= 5  # 上限生效（末块 +1）

    @pytest.mark.asyncio
    async def test_backfill_before_cutoff_skips_primary_overlap(self, tmp_path):
        from datetime import datetime, timedelta
        base = datetime(2026, 6, 1, 9, 0, 0)
        store = self._write_log(tmp_path, 40, base)
        brain = self._summary_brain()
        mem = ShortTermMemory(max_tokens=2000, log_store=store)
        # primary 最旧消息时间落在日志中段 → 回溯只覆盖该时间之前
        m = Message(role="user", content="近期")
        m.timestamp = base + timedelta(minutes=20)
        mem.primary.append(m)
        n = await mem.backfill_tree_from_log(brain, max_leaves=64)
        assert n >= 1
        # 所有节点都应早于 primary 最旧时间（无重叠）
        assert all(node.t_end <= m.timestamp for node in mem.tree.nodes)

    def test_chunk_span_robust_to_bad_ts(self):
        # MEDIUM-2：块内有坏 ts 时，时间范围取可解析 ts 的 min/max，不被坏行拉到未来
        from datetime import datetime
        chunk = [
            {"ts": "2026-06-01T09:00:00"},
            {"ts": "坏的时间戳"},
            {"ts": "2026-06-01T10:00:00"},
            {},  # 缺 ts
        ]
        t0, t1 = ShortTermMemory._chunk_span(chunk)
        assert t0 == datetime(2026, 6, 1, 9, 0, 0)
        assert t1 == datetime(2026, 6, 1, 10, 0, 0)

    @pytest.mark.asyncio
    async def test_backfill_no_log_store_returns_zero(self):
        brain = self._summary_brain()
        mem = ShortTermMemory()  # 无 log_store（如 bubble）
        assert await mem.backfill_tree_from_log(brain) == 0
        assert await mem.backfill_tree_online(brain) == 0

    @pytest.mark.asyncio
    async def test_online_backfill_swap_preserves_newer_live_nodes(self, tmp_path):
        from datetime import datetime, timedelta

        from coworker.memory.memory_tree import MemoryNode
        base = datetime(2026, 6, 1, 9, 0, 0)
        store = self._write_log(tmp_path, 40, base)
        brain = self._summary_brain()
        mem = ShortTermMemory(max_tokens=2000, log_store=store)
        before_t = base + timedelta(minutes=100)
        m = Message(role="user", content="recent")
        m.timestamp = before_t
        mem.primary.append(m)
        # 模拟构建期间主线压缩提升的、晚于 before 的活节点
        live = MemoryNode(level=0, summary="live promoted",
                          t_start=before_t, t_end=before_t + timedelta(minutes=5), msg_count=3)
        mem.tree.nodes.append(live)

        n = await mem.backfill_tree_online(brain, max_leaves=8)
        assert n >= 2  # 从日志回溯出多叶子
        # 晚于 before 的活节点必须在原子替换中保留，且位于末尾（最新）
        assert mem.tree.nodes[-1].summary == "live promoted"
        # 回溯节点（早于 before）在前
        assert all(nd.t_end <= before_t for nd in mem.tree.nodes[:-1])

    @pytest.mark.asyncio
    async def test_online_backfill_swap_keeps_straddling_node(self, tmp_path):
        # HIGH-1 回归：主线合并可能产出一个 t_start<before 但 t_end>=before 的「跨边界」节点
        # （pre-before 老节点 + 刚提升的 post-before 新节点）。按 t_start 会误删它、丢失新内容；
        # 必须按 t_end>=before 保留。
        from datetime import datetime, timedelta

        from coworker.memory.memory_tree import MemoryNode
        base = datetime(2026, 6, 1, 9, 0, 0)
        store = self._write_log(tmp_path, 40, base)
        brain = self._summary_brain()
        mem = ShortTermMemory(max_tokens=2000, log_store=store)
        before_t = base + timedelta(minutes=100)
        m = Message(role="user", content="recent")
        m.timestamp = before_t
        mem.primary.append(m)
        # 跨边界节点：起点早于 before、终点晚于 before（含 post-before 内容）
        straddle = MemoryNode(level=1, summary="straddling promoted",
                              t_start=before_t - timedelta(minutes=10),
                              t_end=before_t + timedelta(minutes=5), msg_count=4)
        mem.tree.nodes.append(straddle)

        n = await mem.backfill_tree_online(brain, max_leaves=8)
        assert n >= 2
        assert any(nd.summary == "straddling promoted" for nd in mem.tree.nodes)  # 未被误删

    @pytest.mark.asyncio
    async def test_online_backfill_no_clobber_when_empty(self, tmp_path):
        from datetime import datetime

        from coworker.agent.log_store import LogStore
        from coworker.memory.memory_tree import MemoryNode
        brain = self._summary_brain()
        mem = ShortTermMemory(max_tokens=2000, log_store=LogStore(tmp_path / "empty"))
        existing = MemoryNode(level=1, summary="existing", t_start=datetime(2026, 6, 1),
                              t_end=datetime(2026, 6, 2), msg_count=5)
        mem.tree.nodes.append(existing)
        n = await mem.backfill_tree_online(brain)
        assert n == 0
        assert [nd.summary for nd in mem.tree.nodes] == ["existing"]  # 活树未被覆盖

    def test_heal_legacy_tree_nodes_on_load(self, tmp_path):
        # 旧版快照里的坏节点：raw_available=False（实则原始在盘上）且 span 反转。
        # deserialize 应自愈：反转 span 规范化为 [min,max]，且经回探日志翻回 raw_available=True。
        from datetime import datetime

        from coworker.memory.memory_tree import MemoryNode
        store = self._write_log(tmp_path, 20, datetime(2026, 6, 1, 9, 0, 0))
        t0 = datetime(2026, 6, 1, 9, 0, 0)
        t1 = datetime(2026, 6, 1, 9, 15, 0)
        mem = ShortTermMemory(log_store=store)
        # 误判为仅摘要、且 span 反转（t_start > t_end）的历史节点
        mem.tree.nodes.append(MemoryNode(level=1, summary="legacy", t_start=t1, t_end=t0,
                                         msg_count=10, raw_available=False))
        data = mem.serialize()
        restored = ShortTermMemory.deserialize(data, log_store=store)
        n = restored.tree.nodes[0]
        assert n.t_start <= n.t_end, "反转 span 应被规范化"
        assert n.raw_available is True, "原始可达应被翻回 True"

    def test_heal_skips_genuinely_unreachable(self, tmp_path):
        # 区间落在日志覆盖范围之外（原始确已不在）→ 不应误翻为 True。
        from datetime import datetime

        from coworker.memory.memory_tree import MemoryNode
        store = self._write_log(tmp_path, 20, datetime(2026, 6, 1, 9, 0, 0))
        far = datetime(2020, 1, 1, 0, 0, 0)
        mem = ShortTermMemory(log_store=store)
        mem.tree.nodes.append(MemoryNode(level=1, summary="archived", t_start=far,
                                         t_end=far, msg_count=10, raw_available=False))
        restored = ShortTermMemory.deserialize(mem.serialize(), log_store=store)
        assert restored.tree.nodes[0].raw_available is False

    def test_recalled_memory_ids_round_trip(self):
        mem = ShortTermMemory()
        mem.primary.append(Message(
            role="user",
            content="[自动回忆] ...",
            recalled_memory_ids=["abc-123", "def-456"],
        ))

        data = mem.serialize()
        restored = ShortTermMemory.deserialize(data)

        assert restored.primary[0].recalled_memory_ids == ["abc-123", "def-456"]

    def test_recalled_memory_ids_default_empty(self):
        mem = ShortTermMemory()
        mem.primary.append(Message(role="user", content="hello"))
        data = mem.serialize()
        restored = ShortTermMemory.deserialize(data)
        assert restored.primary[0].recalled_memory_ids == []

    @pytest.mark.asyncio
    async def test_compress_all_promotes_all_live_primary_to_tree(self):
        brain = self._summary_brain()
        mem = ShortTermMemory()
        for i in range(3):
            mem.primary.append(Message(role="user", content=f"msg {i}"))

        compressed, _ = await mem.compress_all_now(brain)

        assert compressed == 3
        assert mem.primary == []
        assert len(mem.tree.nodes) == 1
        assert mem.tree.nodes[0].msg_count == 3
        assert mem.tree.nodes[0].summary == "节点摘要"
        assert mem.compress_generation == 1

    @pytest.mark.asyncio
    async def test_compress_all_preserves_active_tool_use_tail(self):
        brain = self._summary_brain()
        mem = ShortTermMemory()
        for i in range(3):
            mem.primary.append(Message(role="user", content=f"msg {i}"))
        tail = Message(
            role="assistant",
            content="",
            tool_calls=[{"id": "tc1", "name": "clear_short_term_memory", "arguments": {}}],
        )
        mem.primary.append(tail)

        compressed, _ = await mem.compress_all_now(brain)

        assert compressed == 3
        assert mem.primary == [tail]
        assert len(mem.tree.nodes) == 1
        assert mem.tree.nodes[0].msg_count == 3

    @pytest.mark.asyncio
    async def test_compress_all_bails_if_prefix_mutated_during_summarize(self):
        brain = self._summary_brain()
        mem = ShortTermMemory()
        for i in range(3):
            mem.primary.append(Message(role="user", content=f"msg {i}"))

        async def evil_summarize(messages, context_hint="", **_):
            mem.primary.clear()
            mem.primary.append(Message(role="user", content="brand new"))
            return "mutated"

        brain.summarize = evil_summarize  # type: ignore[method-assign]

        compressed, _ = await mem.compress_all_now(brain)

        assert compressed == 0
        assert len(mem.tree.nodes) == 0
        assert [m.content for m in mem.primary] == ["brand new"]
        assert mem.compress_generation == 0

    @pytest.mark.asyncio
    async def test_compress_all_legacy_single_anchor_preserves_tool_use_tail(self):
        brain = self._summary_brain()
        mem = ShortTermMemory(tree_enabled=False)
        for i in range(2):
            mem.primary.append(Message(role="user", content=f"msg {i}"))
        tail = Message(
            role="assistant",
            content="",
            tool_calls=[{"id": "tc1", "name": "clear_short_term_memory", "arguments": {}}],
        )
        mem.primary.append(tail)

        compressed, _ = await mem.compress_all_now(brain)

        assert compressed == 2
        assert len(mem.primary) == 2
        assert "行动摘要" in mem.primary[0].content
        assert mem.primary[1] is tail
        assert mem.compress_generation == 1


class TestPinnedContext:
    def test_pin_registers_item_without_touching_primary(self):
        mem = ShortTermMemory()
        mem.pin("rules", "编码规范", "不要用 print，用 logging")
        assert len(mem.pinned_items) == 1
        assert mem.pinned_items[0].pin_id == "rules"
        # 新 pin 不立即写入 primary，等下一个 cycle 的 reinject_missing_pins()
        assert len(mem.primary) == 0

    def test_pin_update_existing_replaces_item_without_rewriting_message(self):
        mem = ShortTermMemory()
        mem.pin("rules", "旧标题", "旧内容")
        mem.reinject_missing_pins()              # 补入 primary
        assert "旧内容" in mem.primary[0].content

        mem.pin("rules", "新标题", "新内容")
        assert len(mem.pinned_items) == 1
        assert mem.pinned_items[0].content == "新内容"
        assert len(mem.primary) == 1
        assert "旧内容" in mem.primary[0].content

        # Existing model-visible messages are immutable for provider cache consistency.
        # Once the old pin message leaves primary, reinjection uses the latest pin state.
        mem.primary.clear()
        mem.reinject_missing_pins()
        assert len(mem.primary) == 1
        assert "新标题" in mem.primary[0].content
        assert "新内容" in mem.primary[0].content

    def test_unpin_removes_item_without_rewriting_primary(self):
        mem = ShortTermMemory()
        mem.pin("rules", "规范", "内容")
        mem.reinject_missing_pins()
        visible_pin = mem.primary[0]

        found = mem.unpin("rules")

        assert found is True
        assert len(mem.pinned_items) == 0
        assert mem.primary == [visible_pin]

        mem.primary.clear()
        result = mem.reinject_missing_pins()
        assert result == []
        assert mem.primary == []

    def test_unpin_returns_false_when_not_found(self):
        mem = ShortTermMemory()
        assert mem.unpin("nonexistent") is False

    def test_reinject_missing_pins_noop_when_none(self):
        mem = ShortTermMemory()
        mem.primary.append(Message(role="user", content="hello"))
        result = mem.reinject_missing_pins()
        assert result == []
        assert len(mem.primary) == 1

    def test_reinject_missing_pins_appends_when_absent(self):
        mem = ShortTermMemory()
        mem.pin("rules", "规范", "不要用 print")
        # pin() 不写 primary，reinject_missing_pins() 应补入
        result = mem.reinject_missing_pins()
        assert len(result) == 1
        assert result[0].pin_id == "rules"
        assert len(mem.primary) == 1
        assert mem.primary[0].pin_id == "rules"
        assert "规范" in mem.primary[0].content

    def test_reinject_skips_pins_still_in_primary(self):
        mem = ShortTermMemory()
        mem.pin("rules", "规范", "内容")
        mem.reinject_missing_pins()   # 第一次：补入
        result = mem.reinject_missing_pins()  # 第二次：已存在，跳过
        assert result == []
        assert len(mem.primary) == 1

    def test_serialize_deserialize_preserves_pinned_items(self):
        mem = ShortTermMemory()
        mem.pin("rules", "编码规范", "用 logging")
        data = mem.serialize()
        assert "pinned_items" in data
        restored = ShortTermMemory.deserialize(data)
        assert len(restored.pinned_items) == 1
        assert restored.pinned_items[0].pin_id == "rules"
        assert restored.pinned_items[0].label == "编码规范"

    def test_serialize_deserialize_preserves_pin_id_on_messages(self):
        mem = ShortTermMemory()
        mem.pin("rules", "规范", "内容")
        mem.reinject_missing_pins()  # 先补入 primary，再序列化
        data = mem.serialize()
        restored = ShortTermMemory.deserialize(data)
        pin_msgs = [m for m in restored.primary if m.pin_id == "rules"]
        assert len(pin_msgs) == 1
        assert pin_msgs[0].source == "pinned_context"

    def test_serialize_deserialize_preserves_message_usage(self):
        mem = ShortTermMemory()
        mem.primary.append(Message(
            role="assistant",
            content="done",
            usage={"input_tokens": 321, "output_tokens": 12},
        ))

        restored = ShortTermMemory.deserialize(mem.serialize())

        assert restored.primary[0].usage == {"input_tokens": 321, "output_tokens": 12}

    def test_old_snapshot_without_pinned_items_loads_cleanly(self):
        # 旧快照不含 pinned_items 字段，应正常加载
        data = {"primary": [{"role": "user", "content": "hello"}], "threads": {}}
        restored = ShortTermMemory.deserialize(data)
        assert len(restored.pinned_items) == 0
        assert len(restored.primary) == 1

    def test_load_pin_content_file_pin_reads_file(self, tmp_path):
        mem = ShortTermMemory()
        f = tmp_path / "spec.txt"
        f.write_text("初始内容", encoding="utf-8")
        mem.pin("spec", "规格", "初始内容", file_path=str(f))

        # 修改文件内容
        f.write_text("更新内容", encoding="utf-8")

        # 模拟压缩后重注入
        mem.primary.clear()
        mem.reinject_missing_pins()

        assert "更新内容" in mem.primary[0].content
        assert mem.pinned_items[0].content == "更新内容"

    def test_load_pin_content_fallback_on_missing_file(self, tmp_path):
        mem = ShortTermMemory()
        f = tmp_path / "spec.txt"
        f.write_text("缓存内容", encoding="utf-8")
        mem.pin("spec", "规格", "缓存内容", file_path=str(f))

        # 删除文件后重注入，应使用缓存内容
        f.unlink()
        mem.primary.clear()
        mem.reinject_missing_pins()

        assert "缓存内容" in mem.primary[0].content

    def test_list_pinned_returns_copy(self):
        mem = ShortTermMemory()
        mem.pin("a", "A", "内容A")
        result = mem.list_pinned()
        assert len(result) == 1
        result.clear()
        assert len(mem.pinned_items) == 1  # 原始列表不受影响


class TestClear:
    def test_clear_removes_all_plain_messages(self):
        mem = ShortTermMemory()
        for i in range(5):
            mem.primary.append(Message(role="user", content=f"msg {i}"))
        cleared = mem.clear()
        assert cleared == 5
        assert len(mem.primary) == 0

    def test_clear_preserves_trailing_tool_use_message(self):
        mem = ShortTermMemory()
        for i in range(4):
            mem.primary.append(Message(role="user", content=f"msg {i}"))
        mem.primary.append(Message(
            role="assistant",
            content="",
            tool_calls=[{"id": "tc1", "name": "clear_short_term_memory", "arguments": {}}],
        ))
        cleared = mem.clear()
        assert cleared == 4
        assert len(mem.primary) == 1
        assert mem.primary[0].role == "assistant"
        assert mem.primary[0].tool_calls

    def test_clear_preserves_pinned_items(self):
        mem = ShortTermMemory()
        mem.pin("rules", "规范", "内容")
        mem.primary.append(Message(role="user", content="hello"))
        mem.clear()
        assert len(mem.pinned_items) == 1
        assert mem.pinned_items[0].pin_id == "rules"

    def test_clear_empty_primary_returns_zero(self):
        mem = ShortTermMemory()
        assert mem.clear() == 0
        assert mem.primary == []

    def test_clear_without_tool_use_tail_clears_everything(self):
        mem = ShortTermMemory()
        mem.primary.append(Message(role="user", content="last user msg"))
        cleared = mem.clear()
        assert cleared == 1
        assert mem.primary == []
