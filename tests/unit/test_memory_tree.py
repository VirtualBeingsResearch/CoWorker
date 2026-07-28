from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from coworker.memory.memory_tree import (
    _HEADER_TOKENS,
    _LEAF_BUDGET_FLOOR,
    MemoryBlockTree,
    MemoryNode,
)

BASE = datetime(2026, 6, 7, 9, 0, 0)


async def fake_summarize(text: str, hint: str) -> str:
    # 确定性：摘要 = 输入长度签名，便于断言又不依赖随机/真实 LLM。
    return f"S<{len(text)}>"


# --- 带尺寸的模拟 summarize：CJK 1 字 = 1 token，故按 hint 里「约 N tokens」出尺寸。----
# 用于复现/锁定与 token 体量相关的形态行为（见 scratch/stm_tree_repro.py）。
_BUDGET_RE = re.compile(r"约\s*(\d+)\s*tokens")


def _cjk(n: int) -> str:
    return "概" * max(0, n)


async def sized_summarize(text: str, hint: str) -> str:
    m = _BUDGET_RE.search(hint)
    return _cjk(int(m.group(1)) if m else 200)


def sized_leaves(n: int, *, tokens: int = 300, gap_min: int = 5, msg: int = 10) -> list[MemoryNode]:
    return [
        MemoryNode(
            level=0,
            summary=_cjk(tokens),
            t_start=BASE + timedelta(minutes=i * gap_min),
            t_end=BASE + timedelta(minutes=i * gap_min),
            msg_count=msg,
        )
        for i in range(n)
    ]


def _monotone_non_increasing(levels: list[int]) -> bool:
    """脊柱旧→新 level 单调不增（老粗新细，无 LOD 反转）。"""
    return all(levels[i] >= levels[i + 1] for i in range(len(levels) - 1))


def leaf(minutes: int, msg: int = 10, summary: str = "leaf") -> MemoryNode:
    t = BASE + timedelta(minutes=minutes)
    return MemoryNode(level=0, summary=summary, t_start=t, t_end=t, msg_count=msg)


def new_tree(**kw) -> MemoryBlockTree:
    # 容忍并丢弃已移除的旧参数，省得每个调用点都改（构造器只剩 spine_cap/leaf_budget）。
    for dead in ("seam_gap_seconds", "max_deferrals_per_level", "budget_decay"):
        kw.pop(dead, None)
    defaults = dict(spine_cap_tokens=10_000)
    defaults.update(kw)
    return MemoryBlockTree(**defaults)


class TestCascade:
    @pytest.mark.asyncio
    async def test_small_tree_keeps_leaves_fine(self):
        # 顶层 K 由预算导出、L0 配额 = Fib(K+1) 较大；叶子数少于该配额时全部保留为 L0（细），
        # 不再被同 level 强制进位 —— 近期细节不被过度压缩（Demo② 诉求）。
        tree = new_tree(spine_cap_tokens=10_000)  # K=4 → L0 配额 Fib(5)=5
        for i in range(4):
            await tree.promote_leaf(leaf(i), summarize=fake_summarize)
        assert len(tree.nodes) == 4
        assert all(n.level == 0 for n in tree.nodes)

    @pytest.mark.asyncio
    async def test_pressure_builds_monotone_staircase(self):
        # 预算紧 → 从年老端起合并成「老粗新细」的递减阶梯（旧→新 level 单调不增）。
        tree = new_tree(spine_cap_tokens=1200, leaf_budget_tokens=600, budget_decay=0.7)
        for lf in sized_leaves(13):
            await tree.promote_leaf(lf, sized_summarize)
        levels = [n.level for n in tree.nodes]
        assert len(tree.nodes) > 1
        assert _monotone_non_increasing(levels), levels

    @pytest.mark.asyncio
    async def test_tiny_cap_collapses_and_terminates(self):
        # 极小上限 → K 导出为 1（最小可行脊柱），稳定在 ≤2 节点（{L1, L0}）且终止。
        # 注：旧 enforce_cap 会进一步压到单节点；移除后底由 K=1 决定（2 节点），更合理。
        tree = new_tree(spine_cap_tokens=1)
        for i in range(8):
            await tree.promote_leaf(leaf(i), summarize=fake_summarize)
        assert len(tree.nodes) <= 2
        assert _monotone_non_increasing([n.level for n in tree.nodes])
        assert sum(n.msg_count for n in tree.nodes) == 80  # 覆盖无丢失


class TestFibonacciShape:
    def test_level_allowance_follows_fibonacci(self):
        # 从最高 level（depth 0）往下：1,1,2,3,5,8
        t = new_tree()
        maxL = 5
        allow = [t._level_allowance(maxL - d, maxL) for d in range(6)]
        assert allow == [1, 1, 2, 3, 5, 8]

    @pytest.mark.asyncio
    async def test_scale_shape_is_fibonacci_gradient(self):
        # 规模 + 预算压力下：老→新 level 单调不增；低 level（新、细）节点数多于高 level（老、粗），
        # 顶层 level 不超过预算导出的 K（无暴走 blob），最低 level 保留多个 L0。
        tree = new_tree(spine_cap_tokens=16_000, leaf_budget_tokens=600, budget_decay=0.7)
        K = tree._level_cap()
        for lf in sized_leaves(300):
            await tree.promote_leaf(lf, sized_summarize)
        levels = [n.level for n in tree.nodes]
        assert _monotone_non_increasing(levels), levels
        assert max(levels) <= K  # 顶层封顶、无暴走
        counts: dict[int, int] = {}
        for lv in levels:
            counts[lv] = counts.get(lv, 0) + 1
        assert counts[min(counts)] > counts[max(counts)]  # 新端多、老端少
        assert counts.get(0, 0) >= 5


class TestCapBound:
    @pytest.mark.asyncio
    async def test_no_runaway_under_pressure_no_enforce_cap(self):
        # 去掉 enforce_cap 后：K 进位本身就把脊柱压在 cap 内（spine ≤ cap），节点数 ≤ Fib(K+3)-1。
        tree = new_tree(spine_cap_tokens=16_000, leaf_budget_tokens=600)
        K = tree._level_cap()
        for lf in sized_leaves(300):
            await tree.promote_leaf(lf, sized_summarize)
        assert tree.spine_tokens() <= tree._spine_cap_tokens
        assert len(tree.nodes) <= tree._fib(K + 3) - 1


class TestBudgetRebalance:
    @pytest.mark.asyncio
    async def test_decrease_after_increase_recovers_low_budget_shape(self):
        # 先按小预算运行一段，再放大预算追加更多叶子，最后降回小预算。
        # 重塑后的形态应与「全程使用小预算」从头流式构建一致。
        low_cap, high_cap = 1200, 16_000
        n_before, n_after = 21, 60
        leaves = sized_leaves(n_before + n_after)

        tree = new_tree(spine_cap_tokens=low_cap, leaf_budget_tokens=600)
        low_k = tree._level_cap()
        for lf in leaves[:n_before]:
            await tree.promote_leaf(lf, sized_summarize)

        await tree.rebalance_for_budget(sized_summarize, spine_cap_tokens=high_cap)
        assert tree._level_cap() > low_k
        for lf in leaves[n_before:]:
            await tree.promote_leaf(lf, sized_summarize)
        assert any(n.level > low_k for n in tree.nodes)

        changed = await tree.rebalance_for_budget(sized_summarize, spine_cap_tokens=low_cap)
        assert changed

        reference = new_tree(spine_cap_tokens=low_cap, leaf_budget_tokens=600)
        for lf in leaves:
            await reference.promote_leaf(lf, sized_summarize)

        assert [n.level for n in tree.nodes] == [n.level for n in reference.nodes]
        assert [n.msg_count for n in tree.nodes] == [n.msg_count for n in reference.nodes]
        assert max(n.level for n in tree.nodes) <= tree._level_cap()

    @pytest.mark.asyncio
    async def test_rebalance_shrinks_oversized_nodes_even_when_shape_fits(self):
        # 旧快照可能层级配额已经合规，但单节点摘要远超当前 node_budget，导致 spine_tokens 穿透 cap。
        tree = MemoryBlockTree(spine_cap_tokens=80, leaf_budget_tokens=8)
        tree.nodes = [
            MemoryNode(level=0, summary="巨" * 100, t_start=BASE, t_end=BASE, msg_count=1),
            MemoryNode(level=0, summary="巨" * 100, t_start=BASE, t_end=BASE, msg_count=1),
        ]
        assert tree._next_carry([n.level for n in tree.nodes], tree._level_cap()) is None
        assert tree.needs_rebalance()

        async def concise(text: str, hint: str) -> str:
            return "短"

        changed = await tree.rebalance_for_budget(concise)

        assert changed
        assert not tree.needs_rebalance()
        assert tree.spine_tokens() <= tree._spine_cap_tokens
        assert all(n.token_estimate <= tree.node_budget() for n in tree.nodes)

    @pytest.mark.asyncio
    async def test_rebalance_regenerates_empty_summary_from_children(self):
        # 旧快照里可能出现空摘要父节点；即使 token=0，也必须用保留的子摘要重生成。
        tree = MemoryBlockTree(spine_cap_tokens=400, leaf_budget_tokens=80)
        tree.nodes = [
            MemoryNode(
                level=1,
                summary="",
                t_start=BASE,
                t_end=BASE + timedelta(minutes=5),
                msg_count=2,
                children=[
                    MemoryNode(level=0, summary="创建了多个 bug，剩余一个阻塞待确认", t_start=BASE, t_end=BASE, msg_count=1),
                    MemoryNode(level=0, summary="已完成批量修复，接续是等待验收", t_start=BASE, t_end=BASE, msg_count=1),
                ],
            )
        ]
        assert tree.needs_rebalance()

        async def regenerate(text: str, hint: str) -> str:
            assert "创建了多个 bug" in text
            assert "等待验收" in text
            return "完成一批缺陷修复并保留一个验收阻塞。接续：等待验收确认。"

        changed = await tree.rebalance_for_budget(regenerate)

        assert changed
        assert tree.nodes[0].summary
        assert "接续" in tree.nodes[0].summary
        assert not tree.needs_rebalance()

    @pytest.mark.asyncio
    async def test_promote_leaf_clamps_oversized_leaf_summary(self):
        # 新叶子摘要器若忽略预算，进入树时也必须被本地 token 估算兜底收口。
        tree = MemoryBlockTree(spine_cap_tokens=80, leaf_budget_tokens=8)

        await tree.promote_leaf(
            MemoryNode(level=0, summary="巨" * 100, t_start=BASE, t_end=BASE, msg_count=1),
            fake_summarize,
        )

        assert len(tree.nodes) == 1
        assert tree.nodes[0].token_estimate <= tree.node_budget()
        assert tree.spine_tokens() <= tree._spine_cap_tokens


class TestLeafBudgetDerivation:
    """leaf_budget 由 spine_cap 导出：每节点摘要落在可读区间、脊柱吃满 cap、K 随 cap 动态。"""

    def test_derived_leaf_in_readable_band(self):
        # realistic cap 下，导出的每节点预算落在 ~[floor, floor×φ)（≈400–650），即目标 400–600。
        for cap in (7_200, 24_000, 60_000, 120_000):
            tree = MemoryBlockTree(spine_cap_tokens=cap)  # leaf 自动导出
            leaf = tree.node_budget()
            assert _LEAF_BUDGET_FLOOR <= leaf < _LEAF_BUDGET_FLOOR * 1.62 + 1, (cap, leaf)

    def test_k_is_dynamic_grows_with_cap(self):
        # K 不写死：cap 越大 → 档数越多（_level_cap 算出的 K 单调不减）。
        ks = [MemoryBlockTree(spine_cap_tokens=cap)._level_cap()
              for cap in (7_200, 24_000, 60_000, 120_000)]
        assert ks == sorted(ks) and ks[-1] > ks[0], ks

    def test_derived_fills_cap(self):
        # 导出的 leaf 让满载 spine 贴近 cap（高利用率），且不超 cap。
        for cap in (7_200, 24_000, 60_000):
            tree = MemoryBlockTree(spine_cap_tokens=cap)
            K = tree._level_cap()
            per_node = tree.node_budget() + _HEADER_TOKENS
            full = (tree._fib(K + 3) - 1) * per_node
            assert full <= cap and full >= cap * 0.80, (cap, full)

    def test_explicit_override_wins(self):
        assert MemoryBlockTree(spine_cap_tokens=24_000, leaf_budget_tokens=600).node_budget() == 600


class TestBudgetAndRender:
    def test_node_budget_is_uniform(self):
        # 形状梯度已由斐波那契表达 → 单节点预算统一恒定 = leaf_budget（已改无参）。
        tree = new_tree(leaf_budget_tokens=600)
        assert tree.node_budget() == 600
        assert new_tree(leaf_budget_tokens=222).node_budget() == 222

    @pytest.mark.asyncio
    async def test_render_is_system_role_oldest_first_with_labels(self):
        tree = new_tree()
        await tree.promote_leaf(leaf(0, summary="早"), summarize=fake_summarize)
        await tree.promote_leaf(leaf(120, summary="晚"), summarize=fake_summarize)
        msgs = tree.render()
        assert all(m.role == "system" for m in msgs)
        assert all("[记忆 " in m.content for m in msgs)
        # 与 nodes 同序（旧→新）
        assert len(msgs) == len(tree.nodes)

    def test_render_flags_summary_only_nodes(self):
        # raw_available=False（原始已归档/不可达）→ 渲染标「仅摘要」。
        tree = new_tree()
        tree.nodes = [
            MemoryNode(
                level=1, summary="老", t_start=BASE, t_end=BASE,
                msg_count=2, raw_available=False,
            )
        ]
        msgs = tree.render()
        assert any("仅摘要" in m.content for m in msgs)


class TestMergeReach:
    # 合并不再读原始日志，改为纯摘要叠合；高层向下「够细」reach_depth 层（默认 2=低两层）。
    # 这些用例验证：合并输入来自树内保留的后代子摘要、children 嵌套子树正确填充且剪枝有界、
    # raw_available 纯传播。用极小 cap=1 强制 promote_leaf 在 append 后立即压缩合并。

    @pytest.mark.asyncio
    async def test_merge_uses_summaries_not_raw(self):
        # 合并入参来自源的后代子摘要（叶子时即源自身 summary），绝不含原始日志。
        tree = new_tree(spine_cap_tokens=1)
        inputs: list[str] = []

        async def recording(text: str, hint: str) -> str:
            inputs.append(text)
            return "M"

        await tree.promote_leaf(leaf(0, summary="叶0"), summarize=recording)
        await tree.promote_leaf(leaf(1, summary="叶1"), summarize=recording)
        assert len(tree.nodes) == 1 and tree.nodes[0].level == 1
        # 两个叶子无 children → 合并输入是两个叶子 summary 本身（最细，无法再下探）。
        assert inputs and "叶0" in inputs[0] and "叶1" in inputs[0]

    @pytest.mark.asyncio
    async def test_merge_prompt_preserves_continuity_and_high_signal_anchors(self):
        tree = new_tree(spine_cap_tokens=1)
        hints: list[str] = []

        async def recording(text: str, hint: str) -> str:
            hints.append(hint)
            return "M"

        await tree.promote_leaf(leaf(0, summary="叶0"), summarize=recording)
        await tree.promote_leaf(leaf(1, summary="叶1"), summarize=recording)

        assert hints
        assert "连续记忆" in hints[0]
        assert "硬上限" in hints[0]
        assert "数量/编号降噪" in hints[0]
        assert "多个/一批/多轮/若干" in hints[0]
        assert "只保留未闭环、阻塞、验收依据" in hints[0]
        assert "下一步接续点" in hints[0]
        assert "接续：" in hints[0]
        assert "关键锚点只保留高信号" in hints[0]
        assert "不要堆砌普通关键词" in hints[0]
        assert "不要输出“关键词：”列表" in hints[0]

    @pytest.mark.asyncio
    async def test_children_recorded_on_merge(self):
        # 合并节点的 children 记为本次各源（剪枝后），供再上一层够细 + 树内下钻。
        tree = new_tree(spine_cap_tokens=1)
        await tree.promote_leaf(leaf(0, summary="叶0"), summarize=fake_summarize)
        await tree.promote_leaf(leaf(1, summary="叶1"), summarize=fake_summarize)
        node = tree.nodes[0]
        assert [c.summary for c in node.children] == ["叶0", "叶1"]
        assert all(c.level == 0 and not c.children for c in node.children)

    @staticmethod
    def _k2_tree(reach_depth: int = 2) -> MemoryBlockTree:
        # leaf_budget=8 + header=12 → per_node=20；cap=80 → m_max=4 → K=2（可产生 L2 合并）。
        return MemoryBlockTree(spine_cap_tokens=80, leaf_budget_tokens=8, reach_depth=reach_depth)

    @staticmethod
    def _count_distinct_leaves(call_input: str) -> int:
        return sum(1 for i in range(8) if f"叶{i}" in call_input)

    @pytest.mark.asyncio
    async def test_high_merge_reaches_two_levels_down(self):
        # 高层合并（L1+L1→L2）应展开各 L1 源向下一层的后代，在**单次**合并里拿到 4 个 L0
        # 叶子摘要（低两层），而非只用两个 L1 的 summary——这才是「够细」减轻 telephone 的关键。
        tree = self._k2_tree(reach_depth=2)
        inputs: list[str] = []

        async def recording(text: str, hint: str) -> str:
            inputs.append(text)
            return f"M<{len(inputs)}>"

        for i in range(5):  # 5 个无缝叶子在 K=2 下触发一次 L1+L1→L2
            await tree.promote_leaf(leaf(i, summary=f"叶{i}"), summarize=recording)
        # 某一次合并的输入里含 ≥3 个不同 L0 叶子摘要 → 证明确实向下够到了低两层。
        assert any(self._count_distinct_leaves(c) >= 3 for c in inputs), (
            f"reach_depth=2 应有一次合并够到多个 L0 叶子: {inputs}"
        )

    @staticmethod
    def _max_depth(node) -> int:
        return 0 if not node.children else 1 + max(TestMergeReach._max_depth(c) for c in node.children)

    @pytest.mark.asyncio
    async def test_retains_descendants_to_reach_depth(self):
        # reach_depth=2：L2 节点应在树内保留 2 层后代（子 L1 + 孙 L0），可下钻而不必读原始日志。
        tree = self._k2_tree(reach_depth=2)
        for i in range(5):
            await tree.promote_leaf(leaf(i, summary=f"叶{i}"), summarize=fake_summarize)
        l2 = next(n for n in tree.nodes if n.level == 2)
        assert self._max_depth(l2) == 2, "L2 应保留 2 层后代（子+孙）"
        # 孙辈是 L0 叶子，且确实留存了原始叶子摘要。
        grandkids = [g.summary for c in l2.children for g in c.children]
        assert any("叶" in s for s in grandkids), grandkids

    @pytest.mark.asyncio
    async def test_prune_bounds_subtree_depth(self):
        # 留存深度绝不随历史逐代累积：任何节点的子树深度恒 ≤ reach_depth（这里 2）。
        tree = self._k2_tree(reach_depth=2)
        for i in range(40):  # 大量叶子、多代合并
            await tree.promote_leaf(leaf(i, summary=f"叶{i}"), summarize=fake_summarize)
        assert all(self._max_depth(n) <= 2 for n in tree.nodes), (
            [self._max_depth(n) for n in tree.nodes]
        )

    @pytest.mark.asyncio
    async def test_reach_depth_one_uses_direct_children_only(self):
        # reach_depth=1：高层合并只用直接子摘要、绝不向下展开 → 任何单次合并最多含 2 个叶子摘要
        # （仅 L0+L0→L1 那一类），不会出现一次合并够到 ≥3 个 L0 叶子。
        tree = self._k2_tree(reach_depth=1)
        inputs: list[str] = []

        async def recording(text: str, hint: str) -> str:
            inputs.append(text)
            return f"M<{len(inputs)}>"

        for i in range(5):
            await tree.promote_leaf(leaf(i, summary=f"叶{i}"), summarize=recording)
        assert all(self._count_distinct_leaves(c) <= 2 for c in inputs), (
            f"reach_depth=1 不应向下展开: {inputs}"
        )

    @pytest.mark.asyncio
    async def test_out_of_order_merge_envelope_no_span_inversion(self):
        # 回归：后插入的叶子携带更早时间戳（时间戳非单调）。合并 span 必须取 min/max 包络，
        # 否则 [a.t_start, b.t_end] 反转成空窗。
        tree = new_tree(spine_cap_tokens=1)
        await tree.promote_leaf(leaf(60), summarize=fake_summarize)
        await tree.promote_leaf(leaf(0), summarize=fake_summarize)
        node = tree.nodes[0]
        assert node.t_start <= node.t_end, "合并节点的 span 不应反转"

    @pytest.mark.asyncio
    async def test_merge_keeps_raw_available_by_default(self):
        # 合并不读原文 → raw_available 按子节点 all() 传播；叶子默认可达 → 合并节点可达。
        tree = new_tree(spine_cap_tokens=1)
        await tree.promote_leaf(leaf(0), summarize=fake_summarize)
        await tree.promote_leaf(leaf(1), summarize=fake_summarize)
        assert len(tree.nodes) == 1 and tree.nodes[0].level == 1
        assert tree.nodes[0].raw_available is True

    @pytest.mark.asyncio
    async def test_unavailable_child_propagates(self):
        # 任一源不可达 → 合并结果按 all() 继承 False。
        tree = new_tree(spine_cap_tokens=1)
        # 直接放一个不可达叶子 + 一个可达叶子，触发合并。
        bad = MemoryNode(level=0, summary="坏", t_start=BASE, t_end=BASE,
                         msg_count=1, raw_available=False)
        tree.nodes.append(bad)
        await tree.promote_leaf(leaf(1), summarize=fake_summarize)
        assert len(tree.nodes) == 1 and tree.nodes[0].raw_available is False


class TestHardening:
    @pytest.mark.asyncio
    async def test_merge_clamps_runaway_summary(self):
        # 摘要器失控返回超长文本 → 合并节点摘要被安全阀截断（防脊柱被撑爆）
        tree = new_tree(spine_cap_tokens=1, leaf_budget_tokens=10)  # 紧上限以触发合并

        async def huge(text: str, hint: str) -> str:
            return "巨" * 100_000

        await tree.promote_leaf(leaf(0), summarize=huge)
        await tree.promote_leaf(leaf(1), summarize=huge)  # 触发合并 → clamp
        assert len(tree.nodes) == 1
        assert len(tree.nodes[0].summary) < 1000  # 远小于 100000，被截断

    @pytest.mark.asyncio
    async def test_merge_retries_with_budget_prompt_before_clamping(self):
        tree = new_tree(spine_cap_tokens=1, leaf_budget_tokens=10)
        hints: list[str] = []

        async def too_big_then_fit(text: str, hint: str) -> str:
            hints.append(hint)
            return "巨" * 20 if len(hints) == 1 else "短"

        await tree.promote_leaf(leaf(0), summarize=too_big_then_fit)
        await tree.promote_leaf(leaf(1), summarize=too_big_then_fit)

        assert len(hints) == 2
        assert "上一版摘要约" in hints[1]
        assert "请重新压缩到 10 tokens 以内" in hints[1]
        assert "目标约 8 tokens" in hints[1]
        assert "下一步接续点" in hints[1]
        assert "关键词：" in hints[1]
        assert "默认概括为多个/一批/多轮/若干" in hints[1]
        assert "才保留具体编号" in hints[1]
        assert "最后一句必须是接续状态" in hints[1]
        assert "不要解释" in hints[1]
        assert tree.nodes[0].summary == "短"

    @pytest.mark.asyncio
    async def test_merge_retries_empty_summary(self):
        tree = new_tree(spine_cap_tokens=1, leaf_budget_tokens=10)
        hints: list[str] = []

        async def empty_then_fit(text: str, hint: str) -> str:
            hints.append(hint)
            return "" if len(hints) == 1 else "接续：已重生成。"

        await tree.promote_leaf(leaf(0), summarize=empty_then_fit)
        await tree.promote_leaf(leaf(1), summarize=empty_then_fit)

        assert len(hints) == 2
        assert "上一版摘要为空或不可用" in hints[1]
        assert "不要输出空白" in hints[1]
        assert tree.nodes[0].summary == "接续：已重生成。"

    def test_node_budget_equals_leaf_budget(self):
        tree = new_tree(leaf_budget_tokens=50)
        assert tree.node_budget() == 50

    def test_clone_empty_same_config(self):
        t = new_tree(spine_cap_tokens=1234, leaf_budget_tokens=222)
        t.nodes.append(MemoryNode(level=0, summary="x", t_start=BASE, t_end=BASE, msg_count=1))
        c = t.clone_empty()
        assert c.nodes == []
        assert (c._spine_cap_tokens, c._leaf_budget) == (1234, 222)

    def test_spine_tokens_counts_header_overhead(self):
        tree = new_tree()
        n = MemoryNode(level=0, summary="abc", t_start=BASE, t_end=BASE, msg_count=1)
        tree.nodes = [n]
        assert tree.spine_tokens() > n.token_estimate  # 含头部开销

    def test_token_count_source_serializes_and_legacy_defaults_to_estimated(self):
        n = MemoryNode(
            level=0,
            summary="abc",
            t_start=BASE,
            t_end=BASE,
            msg_count=1,
            token_estimate=7,
            token_count_source="exact",
        )
        assert n.to_dict()["token_count_source"] == "exact"

        restored = MemoryNode.from_dict({
            "level": 0,
            "summary": "legacy",
            "t_start": BASE.isoformat(),
            "t_end": BASE.isoformat(),
            "msg_count": 1,
            "token_estimate": 6,
        })
        assert restored.token_count_source == "estimated"


class TestBuildBalanced:
    @staticmethod
    def _leaves(n, summary="x", gap_min=1):
        return [
            MemoryNode(level=0, summary=summary,
                       t_start=BASE + timedelta(minutes=i * gap_min),
                       t_end=BASE + timedelta(minutes=i * gap_min), msg_count=5)
            for i in range(n)
        ]

    @pytest.mark.asyncio
    async def test_no_collapse_on_power_of_two(self):
        # 8 个无缝叶子：旧流式二进制进位会塌成 1 个（popcount(8)=1）。斐波那契进位不会塌成 1，
        # 保留多节点的老粗新细梯度（与流式同形）；时序保持、level 旧→新单调不增。
        tree = new_tree(spine_cap_tokens=1_000_000)  # 大 cap → 仅斐波那契塑形、不触发硬底
        await tree.build_balanced(self._leaves(8), fake_summarize)
        assert 1 < len(tree.nodes) <= 8
        starts = [n.t_start for n in tree.nodes]
        assert starts == sorted(starts)  # 时序保持
        assert _monotone_non_increasing([n.level for n in tree.nodes])

    @pytest.mark.asyncio
    async def test_reduces_to_fit_cap_but_stays_multinode(self):
        # 预算导出 K=5：16 个叶子归约成比例化斐波那契（多节点、不塌成 1），形状有界。
        tree = new_tree(spine_cap_tokens=16_000)

        async def big(text: str, hint: str) -> str:
            return "概" * 150

        await tree.build_balanced(self._leaves(16, summary="概" * 200), big)
        assert 1 < len(tree.nodes) < 16   # 归约了但没塌成 1
        assert _monotone_non_increasing([n.level for n in tree.nodes])

    @pytest.mark.asyncio
    async def test_fewer_than_two_leaves_noop(self):
        tree = new_tree()
        await tree.build_balanced(self._leaves(1), fake_summarize)
        assert len(tree.nodes) == 1

    @pytest.mark.asyncio
    async def test_offline_shape_equals_streaming(self):
        # 离线 build_balanced 与逐个流式 promote 必须产出一致的 level 形态（共用 _next_carry）。
        cap, N = 16_000, 60
        offline = new_tree(spine_cap_tokens=cap)
        await offline.build_balanced(sized_leaves(N), sized_summarize)

        streamed = new_tree(spine_cap_tokens=cap)
        for lf in sized_leaves(N):
            await streamed.promote_leaf(lf, sized_summarize)

        assert [n.level for n in offline.nodes] == [n.level for n in streamed.nodes]
        assert [n.msg_count for n in offline.nodes] == [n.msg_count for n in streamed.nodes]

    @pytest.mark.asyncio
    async def test_offline_summarize_calls_bounded(self):
        # 离线只对「多叶子最终节点」各 summarize 一次 → 调用数 = 最终多叶子节点数 ≪ 流式 O(N)。
        cap, N = 16_000, 300
        tree = new_tree(spine_cap_tokens=cap)
        calls = 0

        async def counting(text: str, hint: str) -> str:
            nonlocal calls
            calls += 1
            return await sized_summarize(text, hint)

        await tree.build_balanced(sized_leaves(N), counting)
        multi_leaf_nodes = sum(1 for lv, lo, hi in tree._plan_partition(N) if hi > lo)
        assert calls == multi_leaf_nodes
        assert calls < N // 5  # 远少于叶子数（O(最终节点) 而非 O(N)）


class TestLodGradientInvariants:
    """LOD 梯度不变量验收基线（重构验收用）。

    约定的目标形态（与轴=活动距离/level 一致，时间只作边界提示）：
    - 旧→新 level 单调不增（老粗新细），任何路径都不应出现年轻节点 level 反超老节点；
    - 近期细节不被吞：续流式后年轻端仍有细粒度节点；
    - 空闲不驱动压缩：纯时间缝（沉睡）不改变老节点的 level/token。
    复现见 scratch/stm_tree_repro.py。
    """

    @pytest.mark.asyncio
    async def test_streaming_from_empty_is_monotone(self):
        # 纯流式从空建：规范递减阶梯，老→新 level 单调不增。
        tree = new_tree(spine_cap_tokens=1_000_000, leaf_budget_tokens=600, budget_decay=0.7)
        for lf in sized_leaves(13):
            await tree.promote_leaf(lf, sized_summarize)
        levels = [n.level for n in tree.nodes]
        assert _monotone_non_increasing(levels), levels

    @pytest.mark.asyncio
    async def test_backfill_then_stream_keeps_recent_fine(self):
        # 回溯后续流式：最近一条仍应是细粒度（不被立刻吞进老粗 blob）。
        # 这条在当前实现下已通过，是「修复时别把近期细节牺牲掉」的护栏。
        tree = new_tree(spine_cap_tokens=1200, leaf_budget_tokens=600, budget_decay=0.7)
        await tree.build_balanced(sized_leaves(8), sized_summarize)
        await tree.promote_leaf(sized_leaves(9)[8], sized_summarize)
        assert tree.nodes[-1].level == 0

    @pytest.mark.asyncio
    async def test_backfill_then_stream_no_inversion(self):
        # 回溯 8 个 + 续流式 5 个，最终 level 应旧→新单调不增（无反转）。
        tree = new_tree(spine_cap_tokens=1200, leaf_budget_tokens=600, budget_decay=0.7)
        leaves13 = sized_leaves(13)
        await tree.build_balanced(leaves13[:8], sized_summarize)
        for lf in leaves13[8:]:
            await tree.promote_leaf(lf, sized_summarize)
        levels = [n.level for n in tree.nodes]
        assert _monotone_non_increasing(levels), f"LOD 反转: {levels}"

    @pytest.mark.asyncio
    async def test_idle_gap_does_not_change_shape(self):
        # 压缩只看活动量（叶子数），不看 wall-clock：同样 6 个叶子，"中间隔 30 天" 与
        # "全程紧凑相邻" 产出相同的 level 结构 → 沉睡不会额外压缩老记忆。
        async def build(gaps: list[timedelta]) -> list[int]:
            t = new_tree(spine_cap_tokens=1_000_000, leaf_budget_tokens=600, budget_decay=0.7)
            at = BASE
            for g in gaps:
                at = at + g
                await t.promote_leaf(
                    MemoryNode(level=0, summary=_cjk(300), t_start=at, t_end=at, msg_count=10),
                    sized_summarize,
                )
            return [n.level for n in t.nodes]

        five_min = timedelta(minutes=5)
        day_30 = timedelta(days=30)
        dense = await build([five_min] * 6)
        with_gap = await build([five_min, five_min, day_30, five_min, five_min, five_min])
        assert dense == with_gap


class TestSerialize:
    @staticmethod
    def _child_sigs(nodes) -> list[list[str]]:
        return [[c.summary for c in n.children] for n in nodes]

    @pytest.mark.asyncio
    async def test_serialize_round_trip(self):
        # 用小 cap 触发合并，使最老节点带嵌套 children，验证嵌套子树也往返保真。
        tree = new_tree(spine_cap_tokens=1)
        for i in range(5):
            await tree.promote_leaf(leaf(i * 60, summary=f"叶{i}"), summarize=fake_summarize)
        assert any(n.children for n in tree.nodes), "应至少有一个合并节点带 children"
        data = tree.serialize()
        restored = new_tree(spine_cap_tokens=1)
        restored.load(data)
        assert len(restored.nodes) == len(tree.nodes)
        assert [n.level for n in restored.nodes] == [n.level for n in tree.nodes]
        assert restored.nodes[0].t_start == tree.nodes[0].t_start
        # 嵌套子树（含孙辈）逐层保真。
        assert self._child_sigs(restored.nodes) == self._child_sigs(tree.nodes)
        for r, o in zip(restored.nodes, tree.nodes, strict=True):
            assert self._child_sigs(r.children) == self._child_sigs(o.children)

    @pytest.mark.asyncio
    async def test_load_legacy_node_without_children(self):
        # 旧快照无 children 字段 → 反序列化默认空列表，向后兼容。
        restored = new_tree()
        restored.load({"nodes": [{
            "level": 0, "summary": "老", "t_start": BASE.isoformat(),
            "t_end": BASE.isoformat(), "msg_count": 3,
        }]})
        assert restored.nodes[0].children == []
