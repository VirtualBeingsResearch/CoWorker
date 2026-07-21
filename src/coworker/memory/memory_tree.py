from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal

from loguru import logger

from coworker.core.types import Message, SummaryResult, estimate_content_tokens
from coworker.i18n import tr

# (text_to_summarize, context_hint) -> summary text
SummaryLike = str | SummaryResult
SummarizeFn = Callable[[str, str], Awaitable[SummaryLike]]
TokenCountSource = Literal["estimated", "exact"]

# 高层合并向下「够细」的深度：合并某节点时，输入取各源**向下 reach_depth 层**的更细摘要，
# 而非源自身的 summary，把 telephone 退化深度约减半。每个节点同时把它**向下 reach_depth 层的
# 后代子树**（剪枝后）保留在 ``MemoryNode.children`` 里——既作下次合并的够细材料，又让老节点
# 留在树内可下钻/渲染（不必每次回原始日志）。默认 2（即「低两层」：子+孙）；子树有界 ≤ 分支^depth。
_DEFAULT_REACH_DEPTH = 2

# 渲染时每个脊柱节点的固定头部+消息开销（`[记忆 …]` 行 + system 消息封装），计入脊柱预算。
_HEADER_TOKENS = 12
# 合并摘要的硬字符上限 = 预算 token × 此系数：远高于正常中英文摘要（不会截到合规输出），
# 仅作安全阀截断「摘要器失控/回声」式的异常膨胀。去掉 enforce_cap 后这是**唯一**的 per-node
# token 防线——K 进位只界定节点数，单节点体量靠它兜底，避免失控摘要撑爆脊柱。
_SUMMARY_CHARS_PER_TOKEN = 6

# 每节点摘要的目标大小下限（token）。leaf_budget 不是独立旋钮，而是**由 spine_cap 导出**
# （见 `_derive_leaf_budget`）：取「最细粒度（最大 K）而每节点摘要仍 ≥ 此下限」对应的、令脊柱
# 正好填满 cap 的峰值预算。于是单条摘要落在 ~400–600 的可读区间、脊柱吃满 cap，而 K（档数）
# 由 cap 大小自然决定（cap 越大 K 越大）、绝不写死。
_LEAF_BUDGET_FLOOR = 400


def _summary_text_tokens_and_source(result: SummaryLike) -> tuple[str, int, TokenCountSource]:
    if isinstance(result, SummaryResult):
        text = result.content
        tokens = result.output_tokens
        if tokens > 0:
            return text, tokens, "exact"
        return text, estimate_content_tokens(text), "estimated"
    return result, estimate_content_tokens(result), "estimated"


@dataclass
class MemoryNode:
    """记忆块树的一个节点：某段时间内活动的某个分辨率（LOD）下的摘要。

    level 越高越粗、覆盖时间跨度越大。t_start/t_end 取自底层消息的 timestamp，
    是节点的**权威地址**——渲染标签、以及从原始日志按时间区间重摘要都用它。
    （树按时间寻址，不依赖 seq：Message→seq 映射本就不可得，且节点顺序由插入序而非
    时间戳保证。日志条目的 seq 仅供后续分片轮转的 manifest 使用。）
    raw_available 表示该节点对应区间的**原始日志是否仍可下钻**（可达性），由
    ``query_memory(start=..., end=...)`` 时间窗回忆；合并只按子节点 ``all()`` 传播它，不再读原始日志重摘要。
    children 是本节点向下保留的**后代子树**（嵌套 MemoryNode，剪枝到 reach_depth 层、叶子为空）：
    既作更高层合并「向下够细」的输入材料，又让老节点留在树内可下钻/渲染，不必每次回原始日志。
    token_estimate 为兼容旧快照保留字段名；token_count_source 标明该值来自本地估算
    (estimated) 还是模型 usage 的输出 token (exact)。
    """

    level: int
    summary: str
    t_start: datetime
    t_end: datetime
    msg_count: int
    token_estimate: int = 0
    token_count_source: TokenCountSource = "estimated"
    raw_available: bool = True
    children: list[MemoryNode] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.token_estimate:
            self.token_estimate = estimate_content_tokens(self.summary)
            self.token_count_source = "estimated"

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "summary": self.summary,
            "t_start": self.t_start.isoformat(),
            "t_end": self.t_end.isoformat(),
            "msg_count": self.msg_count,
            "token_estimate": self.token_estimate,
            "token_count_source": self.token_count_source,
            "raw_available": self.raw_available,
            "children": [c.to_dict() for c in self.children],
        }

    def clone(self) -> MemoryNode:
        """深拷贝节点及其保留的子摘要树。"""
        return replace(self, children=[c.clone() for c in self.children])

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MemoryNode:
        return cls(
            level=d["level"],
            summary=d["summary"],
            t_start=datetime.fromisoformat(d["t_start"]),
            t_end=datetime.fromisoformat(d["t_end"]),
            msg_count=d.get("msg_count", 0),
            token_estimate=d.get("token_estimate", 0),
            token_count_source=d.get("token_count_source", "estimated"),
            raw_available=d.get("raw_available", True),
            children=[cls.from_dict(c) for c in d.get("children", [])],
        )

    def span_label(self) -> str:
        s, e = self.t_start, self.t_end
        if s.date() == e.date():
            return f"{s.strftime('%m-%d %H:%M')}–{e.strftime('%H:%M')}"
        return f"{s.strftime('%m-%d %H:%M')}–{e.strftime('%m-%d %H:%M')}"


class MemoryBlockTree:
    """多分辨率时间记忆脊柱：斐波那契配额的 level 进位（塑形）+ 预算导出的顶层上限 K（有界）。

    ``nodes`` 始终按时间旧→新（即粗→细）维护，等于插入顺序，无需依赖（可能非单调的）
    时间戳排序。新叶子从年轻端进入、做 **level 进位**塑形：每个 level 允许的节点数按**斐波那契**
    放宽——从顶层 K（最老最粗）往下是 1,1,2,3,5,8,…（``_level_allowance``）。某 level 超额则把它
    最老的相邻两个合并进位到上一层；进位越过 K 则**封顶在 K**（最古节点吸收溢出、level 不爬升）。
    于是脊柱呈「老端少而粗、新端多而细」的梯度，节点数恒 ≤ ``Fib(K+3)-1``：比二进制进位
    （每 level ≤1、~log2(N) 个节点、塌得太狠）保留更多近期分辨率，又不像均匀铺砖那样抹平梯度；
    近期多个 L0 叶子得以原样保留（不被同 level 强制进位）。level 旧→新单调非增 → 同 level 连续成段。

    顶层 K 由预算导出（``_level_cap``：``Fib(K+3)-1 ≤ cap/单节点预算``），又因 ``node_budget``
    统一恒定，故 ``spine_tokens ≈ 节点数 × 预算 ≤ cap`` **由 K 进位自然保证**——无需额外的
    token 硬底循环（旧 ``enforce_cap`` 已移除；体量防线只剩 ``_clamp_summary`` 字符安全阀）。

    关键：进位只看「活动距离」(各 level 累计了多少节点)，**绝不看 wall-clock 年龄**——
    沉睡一个月不会触发任何合并，老记忆不因空闲而变粗；只有真有新活动堆进来才推进进位。
    流式 ``promote_leaf`` 与历史回溯 ``build_balanced`` 共用同一套进位决策（``_next_carry``），
    故两者形态完全一致，不存在「回溯扁平 vs 流式阶梯」的断层。

    合并**不读原始日志**：只对已有摘要做叠合（``_summarize_span``）。高层合并会向下展开
    reach_depth 层后代（``_descend_summaries``）拿到更细材料，把 telephone 退化深度约减半；每个
    节点同时把这 reach_depth 层后代子树（``_prune`` 剪枝、有界 ≤ 分支^depth）保留在 ``children``
    里，使老节点留在树内可下钻/渲染。合并入参与子树大小都只随 reach_depth 走、与时间跨度无关——
    这正是「已压缩信息存起来供更高层用」、消灭重读整段原文。
    """

    def __init__(
        self,
        spine_cap_tokens: int = 32_000,
        leaf_budget_tokens: int | None = None,
        reach_depth: int = _DEFAULT_REACH_DEPTH,
    ) -> None:
        """``spine_cap_tokens`` 是唯一的预算旋钮（硬约束）。``leaf_budget_tokens`` 默认 None →
        由 cap **内部导出**（见 ``_derive_leaf_budget``）；传具体值则作显式覆盖（测试/高级用法）。
        ``reach_depth`` 控制高层合并向下够细、以及节点保留后代子树的层数：2=「低两层」（默认，
        子+孙）、1=仅直接子。也是 ``children`` 子树的剪枝深度上限（越大越保真、快照越大）。
        """
        self.nodes: list[MemoryNode] = []
        self._spine_cap_tokens = spine_cap_tokens
        self._leaf_budget_override = leaf_budget_tokens
        self._leaf_budget = (
            leaf_budget_tokens
            if leaf_budget_tokens is not None
            else self._derive_leaf_budget(spine_cap_tokens)
        )
        self._reach_depth = max(1, reach_depth)

    # ---- 预算 ------------------------------------------------------------

    @staticmethod
    def _derive_leaf_budget(spine_cap_tokens: int) -> int:
        """锁定 cap、由它确定 leaf_budget；**K（档数）不写死，仍由 ``_level_cap`` 动态算出**。

        经验（见 scratch/stm_leaf_sweep.py）：固定 cap 后，稳态 ``spine ≈ 节点数 × 每节点预算``
        是关于 leaf_budget 的稳定函数；且对每个 level 上限 K，存在令「满载斐波那契形状正好填满
        cap」的峰值预算 ``peak(K) = cap/(Fib(K+3)-1) - header``（此处 spine≈cap、利用率最高）。
        peak 随 K 递减、相邻峰约差 φ≈1.6 倍——所以「spine≈cap」本身是多解的（锯齿每个峰都贴 cap，
        只是粒度不同），还需再定一维：**取最细粒度（最大 K）而每节点摘要仍 ≥ ``_LEAF_BUDGET_FLOOR``
        的那个峰**。于是 leaf 落在 ~400–600 可读区间、脊柱吃满 cap，K 随 cap 增大而自然增大。
        """

        def peak(k: int) -> int:
            return spine_cap_tokens // (MemoryBlockTree._fib(k + 3) - 1) - _HEADER_TOKENS

        k = 1
        while peak(k + 1) >= _LEAF_BUDGET_FLOOR:
            k += 1
        return max(1, peak(k))  # cap 极小时 peak 可能 < floor，按实际峰值（仍保证 spine ≤ cap）

    def node_budget(self) -> int:
        """每个节点摘要的目标 token 预算：**统一恒定 = leaf_budget**（由 cap 导出，见
        ``_derive_leaf_budget``）。

        粗细梯度已由斐波那契「形状」（每 level 的节点数：老端少、新端多）表达，故无需再按 level
        衰减单个节点的预算——那会对最粗端二次压缩、且让 token 与节点数脱钩。统一预算下
        ``spine_tokens ≈ 节点数 × (预算+头部)``，使 token 上限直接等价于**节点数上限**，
        与 ``_level_cap`` 的 K 导出闭环：K 进位即自然把脊柱压在 cap 内。
        """
        return self._leaf_budget

    def _set_budget(
        self,
        spine_cap_tokens: int | None = None,
        leaf_budget_tokens: int | None = None,
    ) -> None:
        """更新预算配置；未显式传 leaf 时保留显式 override，否则按 cap 重新导出。"""
        if spine_cap_tokens is not None:
            self._spine_cap_tokens = spine_cap_tokens
        if leaf_budget_tokens is not None:
            self._leaf_budget_override = leaf_budget_tokens
        self._leaf_budget = (
            self._leaf_budget_override
            if self._leaf_budget_override is not None
            else self._derive_leaf_budget(self._spine_cap_tokens)
        )

    # ---- 合并 ------------------------------------------------------------

    def _descend_summaries(self, node: MemoryNode, levels: int) -> list[str]:
        """``node`` 向下 ``levels`` 层的摘要——合并「够细」的输入材料。到达叶子或 levels 用尽即停
        在该 frontier（用 node 自身 summary）。流式两源、各展开 ≤ 分支^levels 个 → 入参有界。
        """
        if levels <= 0 or not node.children:
            return [node.summary]
        out: list[str] = []
        for c in node.children:
            out.extend(self._descend_summaries(c, levels - 1))
        return out

    @staticmethod
    def _summary_target_tokens(budget: int) -> int:
        """给 LLM 的目标值低于硬上限，避免贴着预算写完后被本地截断。"""
        return max(8, int(budget * 0.80))

    @staticmethod
    def _summary_is_empty(summary: str) -> bool:
        return not summary.strip()

    @staticmethod
    def _empty_summary_fallback() -> str:
        return tr("memory.tree.empty_fallback")

    def _prune(self, node: MemoryNode, depth: int) -> MemoryNode:
        """复制 ``node``，仅保留其向下 ``depth`` 层的后代子树（更深的丢弃）。

        必须剪枝（而非直接引用源）：源自身已带 reach_depth 层后代，若直接挂为子节点，留存深度会
        逐代累积成无界。每次合并把源剪到「比父少一层」，使父的子树恒为 reach_depth 层、有界。
        """
        if depth <= 0 or not node.children:
            return replace(node, children=[])
        return replace(node, children=[self._prune(c, depth - 1) for c in node.children])

    async def _summarize_span(
        self,
        sources: list[MemoryNode],
        level: int,
        summarize: SummarizeFn,
    ) -> MemoryNode:
        """把若干相邻源节点（已知目标 ``level``）归约成一个更粗节点——**纯摘要叠合、不读原始日志**。

        流式 ``_merge``（两两进位）与离线 ``build_balanced``（一次性把一组叶子归约成一个节点）
        共用此函数。输入取各源向下 ``reach_depth-1`` 层的更细摘要（``_descend_summaries``，即比合并
        目标低 reach_depth 层），summarize 成更高层概要；新节点 ``children`` 记为各源剪枝到
        ``reach_depth-1`` 层后的子树（``_prune``），使本节点子树恒为 reach_depth 层、有界。

        ``raw_available`` 表达「该区间原始日志是否仍可下钻」（可达性），按各源 ``all()`` 传播——
        合并不读原文，故只要源可达、合并结果即可达；时间窗回忆仍由 ``query_memory(start=..., end=...)`` 走 log_store 兜底。
        """
        budget = self.node_budget()
        target = self._summary_target_tokens(budget)
        # 时间戳非单调（脊柱靠插入序保序，见类 docstring）：取所有源四端点的 **min/max 包络**，
        # 而非 [首.t_start, 尾.t_end]——后者在乱序时反转成空窗、span 反转。
        t_start = min(min(s.t_start, s.t_end) for s in sources)
        t_end = max(max(s.t_start, s.t_end) for s in sources)
        msg_count = sum(s.msg_count for s in sources)
        span = tr("memory.tree.span", start=t_start.isoformat(), end=t_end.isoformat())

        reach = self._reach_depth - 1
        inputs: list[str] = []
        for s in sources:
            inputs.extend(self._descend_summaries(s, reach))
        combined = "\n\n".join(
            tr("memory.tree.summary_piece", index=i + 1, text=text) for i, text in enumerate(inputs)
        )
        hint = tr(
            "memory.tree.merge_hint",
            span=span,
            target=target,
            budget=budget,
        )
        summary, token_count, token_source = await self._summarize_with_budget(
            combined, hint, budget, summarize
        )

        return MemoryNode(
            level=level,
            summary=summary,
            t_start=t_start,
            t_end=t_end,
            msg_count=msg_count,
            token_estimate=token_count,
            token_count_source=token_source,
            raw_available=all(s.raw_available for s in sources),
            children=[self._prune(s, reach) for s in sources],
        )

    async def _merge(
        self,
        a: MemoryNode,
        b: MemoryNode,
        summarize: SummarizeFn,
    ) -> MemoryNode:
        """流式两两合并相邻节点 a(老)、b(新) 为更粗一层（``max(level)+1``）。"""
        return await self._summarize_span([a, b], max(a.level, b.level) + 1, summarize)

    @staticmethod
    def _clamp_summary(summary: str, budget_tokens: int) -> str:
        cap = budget_tokens * _SUMMARY_CHARS_PER_TOKEN
        return summary if len(summary) <= cap else summary[:cap] + tr("memory.tree.truncated")

    @staticmethod
    def _clamp_summary_to_estimate(summary: str, budget_tokens: int) -> str:
        """按本地 token 估算强制收进预算，供旧快照迁移/异常摘要兜底。"""
        summary = MemoryBlockTree._clamp_summary(summary, budget_tokens)
        if estimate_content_tokens(summary) <= budget_tokens:
            return summary

        suffix = tr("memory.tree.truncated")
        suffix_tokens = estimate_content_tokens(suffix)
        if suffix_tokens >= budget_tokens:
            lo, hi = 0, len(summary)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if estimate_content_tokens(summary[:mid]) <= budget_tokens:
                    lo = mid
                else:
                    hi = mid - 1
            return summary[:lo]

        target = max(1, budget_tokens - suffix_tokens)
        lo, hi = 0, len(summary)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if estimate_content_tokens(summary[:mid]) <= target:
                lo = mid
            else:
                hi = mid - 1
        return summary[:lo] + suffix

    async def _summarize_with_budget(
        self,
        text: str,
        hint: str,
        budget: int,
        summarize: SummarizeFn,
    ) -> tuple[str, int, TokenCountSource]:
        """让摘要器主动压进预算；若仍失控，最后用本地估算硬兜底。"""
        prompt = hint
        last = ""
        last_tokens = 0
        last_source: TokenCountSource = "estimated"
        for _ in range(3):
            result = await summarize(text, prompt)
            last, last_tokens, last_source = _summary_text_tokens_and_source(result)
            if not self._summary_is_empty(last) and last_tokens <= budget:
                return last, last_tokens, last_source
            if self._summary_is_empty(last):
                prompt = tr(
                    "memory.retry_empty",
                    context=hint,
                    target=self._summary_target_tokens(budget),
                    budget=budget,
                )
                continue
            prompt = tr(
                "memory.retry_over",
                context=hint,
                last_tokens=last_tokens,
                budget=budget,
                target=self._summary_target_tokens(budget),
            )
        logger.warning(
            f"Memory tree summary remained over budget after retries: {last_tokens}>{budget} tokens"
        )
        if self._summary_is_empty(last):
            fallback = self._empty_summary_fallback()
            return fallback, estimate_content_tokens(fallback), "estimated"
        clamped = self._clamp_summary_to_estimate(last, budget)
        if clamped != last:
            return clamped, estimate_content_tokens(clamped), "estimated"
        return clamped, last_tokens, last_source

    async def _resummarize_node_to_budget(
        self,
        node: MemoryNode,
        summarize: SummarizeFn,
    ) -> MemoryNode:
        """把单个旧节点重新压到当前 node_budget，保留其时间窗与子摘要树。"""
        budget = self.node_budget()
        target = self._summary_target_tokens(budget)
        inputs = (
            self._descend_summaries(node, self._reach_depth) if node.children else [node.summary]
        )
        inputs = [text for text in inputs if not self._summary_is_empty(text)]
        if not inputs:
            summary = self._empty_summary_fallback()
            return replace(
                node,
                summary=summary,
                token_estimate=estimate_content_tokens(summary),
            )
        combined = "\n\n".join(
            tr("memory.tree.memory_piece", index=i + 1, text=text) for i, text in enumerate(inputs)
        )
        hint = tr(
            "memory.tree.resummarize_hint",
            target=target,
            budget=budget,
        )
        summary, token_count, token_source = await self._summarize_with_budget(
            combined, hint, budget, summarize
        )
        return replace(
            node,
            summary=summary,
            token_estimate=token_count,
            token_count_source=token_source,
        )

    def _clamp_node_summary_to_budget(self, node: MemoryNode) -> MemoryNode:
        """把新进入树的节点摘要收进当前单节点预算。"""
        budget = self.node_budget()
        if budget < 8 or node.token_estimate <= budget:
            return node
        summary = self._clamp_summary_to_estimate(node.summary, budget)
        return replace(
            node,
            summary=summary,
            token_estimate=estimate_content_tokens(summary),
            token_count_source="estimated",
        )

    # ---- 斐波那契配额的 level 进位 ----------------------------------------

    @staticmethod
    def _fib(k: int) -> int:
        """Fib(1)=1, Fib(2)=1, Fib(3)=2, Fib(4)=3, Fib(5)=5, …（k<=2 记 1）。"""
        if k <= 2:
            return 1
        a, b = 1, 1
        for _ in range(k - 2):
            a, b = b, a + b
        return b

    def _level_allowance(self, level: int, top_level: int) -> int:
        """某 level 允许的最大节点数：从顶层 ``top_level`` 往下按斐波那契放宽 1,1,2,3,5,8,…

        顶层（最老最粗）配额 1，越往低（越新越细）配额越大 → 「老端少而粗、新端多而细」的梯度。
        比二进制进位（每 level 至多 1 个、~log2(N) 个、塌得太狠）保留更多近期分辨率，又不像均匀
        铺砖那样抹平梯度；近期多个 L0 叶子也得以原样保留（不被同 level 强制进位）。
        """
        return self._fib(top_level - level + 1)

    def _level_cap(self) -> int:
        """由预算导出的**顶层 level 上限 K**：满载斐波那契形状的节点数 Fib(K+3)-1 不超过预算
        可容纳的节点数（≈ cap / 单节点预算）。

        于是脊柱形状恒为「比例化的斐波那契」{K:1, K-1:1, K-2:2, …, 0:Fib(K+1)}，节点数 ≈
        Fib(K+3)-1 与 N 无关；历史无限增长时由**顶层 K 节点吸收溢出**（合并封顶在 K，不再
        无界爬升、也不塌成单点 blob 啃穿中间层）。预算越大 → K 越大 → 保留的分辨率越多。
        """
        per_node = self._leaf_budget + _HEADER_TOKENS
        m_max = max(2, self._spine_cap_tokens // per_node)
        k = 1
        while self._fib((k + 1) + 3) - 1 <= m_max:
            k += 1
        return k

    def _next_carry(self, levels: list[int], top_level: int) -> int | None:
        """斐波那契配额进位的**决策**：返回最低超额 level 的最老节点下标（与其右邻合并），
        无超额返回 None。流式 ``_fib_carry`` 与离线 ``_plan_partition`` 共用，保证两者同形。

        节点 level 旧→新单调非增 → 同 level 连续成段；取该段段首即「最老的同 level 节点」，
        与右邻合并保持相邻与单调。
        """
        counts: dict[int, int] = {}
        for lv in levels:
            counts[lv] = counts.get(lv, 0) + 1
        for level in sorted(counts):  # 低 → 高
            if counts[level] > self._level_allowance(level, top_level):
                return next(i for i, lv in enumerate(levels) if lv == level)
        return None

    async def promote_leaf(
        self,
        node: MemoryNode,
        summarize: SummarizeFn,
    ) -> None:
        """把一个 level-0 叶子推入脊柱（年轻端），按斐波那契配额做 level 进位塑形。
        节点数恒 ≤ ``Fib(K+3)-1`` → 脊柱 token 由 K 自然压在 cap 内，无需额外硬底。"""
        node = self._clamp_node_summary_to_budget(node)
        self.nodes.append(node)
        await self._fib_carry(summarize)

    async def _fib_carry(
        self,
        summarize: SummarizeFn,
    ) -> None:
        """维持「每个 level 的节点数 ≤ 斐波那契配额」（配额从预算导出的顶层 K 往下算）：哪个
        level 超额就把它**最老的**相邻两节点合并进位；进位若越过 K 则**封顶在 K**（最古节点吸收
        溢出、level 不再爬升），级联直到各 level 都不超额。形状恒为比例化斐波那契、节点数有界。
        """
        K = self._level_cap()
        guard = 0
        while len(self.nodes) >= 2:
            i = self._next_carry([n.level for n in self.nodes], K)
            if i is None:
                break
            merged = await self._merge(self.nodes[i], self.nodes[i + 1], summarize)
            if merged.level > K:
                merged.level = K  # 顶层封顶：最古 blob 吸收溢出，level 不越过 K
            self.nodes[i : i + 2] = [merged]
            guard += 1
            if guard > 4096:  # 防御性兜底，正常永不触达
                logger.error("MemoryBlockTree._fib_carry exceeded guard, stopping")
                break

    def needs_rebalance(self) -> bool:
        """当前节点形态是否已不满足当前预算导出的 K/配额。"""
        if self.spine_tokens() > self._spine_cap_tokens:
            return True
        if any(self._summary_is_empty(n.summary) for n in self.nodes):
            return True
        if any(n.token_estimate > self.node_budget() for n in self.nodes):
            return True
        if len(self.nodes) < 2:
            return any(n.level > self._level_cap() for n in self.nodes)
        K = self._level_cap()
        levels = [min(n.level, K) for n in self.nodes]
        return levels != [n.level for n in self.nodes] or self._next_carry(levels, K) is not None

    async def rebalance_for_budget(
        self,
        summarize: SummarizeFn,
        *,
        spine_cap_tokens: int | None = None,
        leaf_budget_tokens: int | None = None,
    ) -> bool:
        """按当前/新预算把已有脊柱重塑到斐波那契配额的不动点。

        预算降低时，曾在大预算下产生的高 level 节点可能已经越过新的顶层 K；
        先把它们封顶到 K，再继续执行同一套进位，能恢复到「用该小预算从头流式构建」
        等价的节点分区。预算升高不强行拆分既有粗节点，只影响后续叶子与未来重塑。
        返回值表示形态或预算配置是否发生变化。
        """
        old_signature = (
            self._spine_cap_tokens,
            self._leaf_budget_override,
            self._leaf_budget,
            [
                (n.level, n.msg_count, n.t_start, n.t_end, n.token_estimate, n.summary)
                for n in self.nodes
            ],
        )
        self._set_budget(spine_cap_tokens, leaf_budget_tokens)

        K = self._level_cap()
        for n in self.nodes:
            if n.level > K:
                n.level = K
        await self._fib_carry(summarize)
        for i, n in enumerate(list(self.nodes)):
            if n.token_estimate > self.node_budget() or self._summary_is_empty(n.summary):
                self.nodes[i] = await self._resummarize_node_to_budget(n, summarize)

        new_signature = (
            self._spine_cap_tokens,
            self._leaf_budget_override,
            self._leaf_budget,
            [
                (n.level, n.msg_count, n.t_start, n.t_end, n.token_estimate, n.summary)
                for n in self.nodes
            ],
        )
        return new_signature != old_signature

    def _plan_partition(self, n_leaves: int) -> list[tuple[int, int, int]]:
        """无 LLM 地模拟流式斐波那契进位，返回最终每个节点 ``(level, lo, hi)``——覆盖原始叶子
        下标连续区间 ``[lo, hi]``。与 ``_fib_carry`` 共用 ``_next_carry`` 决策、同样逐叶子
        append+进位到不动点，故离线 ``build_balanced`` 的形状与逐个流式 promote **完全一致**。
        """
        K = self._level_cap()
        plan: list[list[int]] = []  # 每项 [level, lo, hi]
        for idx in range(n_leaves):
            plan.append([0, idx, idx])
            guard = 0
            while len(plan) >= 2:
                i = self._next_carry([p[0] for p in plan], K)
                if i is None:
                    break
                a, b = plan[i], plan[i + 1]
                plan[i : i + 2] = [[min(max(a[0], b[0]) + 1, K), a[1], b[2]]]
                guard += 1
                if guard > 4096:  # 防御性兜底，正常永不触达
                    break
        return [(lv, lo, hi) for lv, lo, hi in plan]

    async def build_balanced(
        self,
        leaves: list[MemoryNode],
        summarize: SummarizeFn,
        concurrency: int = 5,
    ) -> None:
        """从一批 L0 叶子一次性建脊柱（历史回溯）——**离线算法**：先用无 LLM 的进位模拟
        （``_plan_partition``）算出最终 leaf→node 分组，再对每个最终节点**只 summarize 一次**
        （整组一并归约，而非逐对叠摘要）。

        相比逐个 promote 的 O(N) 次合并，summarize 调用数降到 O(最终节点数 ≈ Fib(K+3)-1)；
        且整组叶子一次归约（叶子已是从原文一次性忠实压成）消除了逐对叠摘要的 telephone 退化。
        形状与流式同构（共用 ``_next_carry``），故回溯树与续流式之间无断层。各最终节点彼此独立，
        ``concurrency`` 控制并行摘要上限。最终节点 ``children`` 由 ``_summarize_span`` 记为其各叶子
        （叶子组直接构成本节点，故续流式时它们即低一层的够细材料、也是树内可下钻的老节点）。
        """
        leaves = [self._clamp_node_summary_to_budget(n) for n in leaves]
        if len(leaves) < 2:
            self.nodes = leaves
            return
        plan = self._plan_partition(len(leaves))
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _materialize(level: int, lo: int, hi: int) -> MemoryNode:
            group = leaves[lo : hi + 1]
            if len(group) == 1:
                return group[0]  # 单叶子原样保留，无需 summarize
            async with sem:
                return await self._summarize_span(group, level, summarize)

        self.nodes = list(await asyncio.gather(*(_materialize(lv, lo, hi) for lv, lo, hi in plan)))

    # ---- 渲染与度量 ------------------------------------------------------

    def spine_tokens(self) -> int:
        # 计入每节点渲染头部开销，使度量对照真实注入体量、而非仅摘要正文。
        return sum(n.token_estimate + _HEADER_TOKENS for n in self.nodes)

    def render(self) -> list[Message]:
        """脊柱渲染为上下文前缀消息，旧→新，每条带时间标签。

        用 role="system" 以避开 brain._prepend_timestamps 对 user 消息的二次时间戳前缀。
        """
        out: list[Message] = []
        for n in self.nodes:
            flag = "" if n.raw_available else tr("memory.tree.summary_only")
            header = tr(
                "memory.tree.header",
                span=n.span_label(),
                count=n.msg_count,
                flag=flag,
            )
            out.append(Message(role="system", content=f"{header}\n{n.summary}"))
        return out

    # ---- 持久化 ----------------------------------------------------------

    def reset(self) -> None:
        """清空脊柱（保留配置）。供历史回溯重建。"""
        self.nodes = []

    def clone_empty(self) -> MemoryBlockTree:
        """返回一棵配置相同的空树。供在线回溯先建临时树、再原子替换。"""
        return MemoryBlockTree(
            spine_cap_tokens=self._spine_cap_tokens,
            leaf_budget_tokens=self._leaf_budget_override,
            reach_depth=self._reach_depth,
        )

    def serialize(self) -> dict[str, Any]:
        return {"nodes": [n.to_dict() for n in self.nodes]}

    def load(self, data: dict[str, Any]) -> None:
        # "deferrals" 是旧快照字段，惰性压缩已不用；读时直接忽略，向后兼容。
        self.nodes = [MemoryNode.from_dict(d) for d in data.get("nodes", [])]
