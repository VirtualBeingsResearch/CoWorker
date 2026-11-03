"""上下文预算账本：把模型能自己写进上下文的内容纳入统一计量，超限时提醒。

记忆脊柱有结构性预算（``MemoryBlockTree`` 的 ``spine_cap_tokens``，由斐波那契 K 进位
保证），但模型自己维护的 ``data/thinking.md``、pin、以及系统提示词与工具定义没有共同的
账本：它们没有上限，也没有任何反馈告诉模型"你写得太多了"。这里把**压缩砍不掉的那部分**
（脊柱 + pin + 提示词与工具定义）算成一个比例，按档位通过 inbound 给模型一次带数字的提醒。

设计边界：

- **只提醒，不拦截**。硬约束仍是脊柱的 K 进位和 ``manage_pinned_context`` 的
  5k/15k 上限；这里只负责让模型看见成本，不新增写入闸门。
- 分母固定为 ``ShortTermMemory.max_tokens``（压缩预算），与脊柱 cap 同源；阈值由
  ``MEMORY__CONTEXT_BUDGET_WARN_THRESHOLDS`` 配置，默认 ``0.40/0.45/0.48``。
  默认窗口下"不可压缩部分"的可用区间很窄：脊柱上限 0.30 + 固定提示词与工具约
  0.07–0.09（中英差异）≈ 0.37–0.39 起步，再加满 pin（上限 0.125）≈ 0.50 封顶。
  档位贴着这个区间排才有区分度；高到 0.75 之类的阈值在默认配置下永远打不到。
- 两次压缩之间（``compress_generation`` 不变）最多提醒 3 次；档位越高允许的次数越多，
  相邻提醒至少间隔若干 cycle；比例回落到安全区或发生下一次压缩时计数清零。
- 所有数字都来自 Coworker 内部估算（``estimate_text_tokens``），提醒正文会写明这一点。

另一个容易踩的点：自动压缩每次只切走 primary 最老的 ``compress_ratio``（默认 30%），
并非清空。所以压缩后的**整请求**在稳态下仍占窗口的 70–85%，用 total input tokens 做
阈值没有区分度——这也是这里改用"不可压缩部分"作分子的原因。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from coworker.core.token_utils import estimate_text_tokens

if TYPE_CHECKING:
    from coworker.memory.short_term import ShortTermMemory

# 档位阈值默认值：不可压缩部分 / 压缩预算。实际取值来自
# ``MEMORY__CONTEXT_BUDGET_WARN_THRESHOLDS``；这里只是配置缺省与测试基准。
# 0.40 是"模型自写内容开始有分量"，0.48 已接近默认配置的天花板（≈0.50）。
BUDGET_BANDS: tuple[float, ...] = (0.40, 0.45, 0.48)

# 两次压缩之间的提醒次数上限。
MAX_BUDGET_WARNINGS = 3

# 相邻两次采样/提醒的最小 cycle 间隔，避免短时间内连续刷屏。
MIN_BUDGET_WARNING_GAP_CYCLES = 8


def band_for(ratio: float, bands: tuple[float, ...] = BUDGET_BANDS) -> int:
    """把比例映射到档位：0=安全区，1..len(bands)=逐级超限。"""
    band = 0
    for index, threshold in enumerate(bands, start=1):
        if ratio >= threshold:
            band = index
    return band


def measure_pin_tokens(short_term: ShortTermMemory) -> tuple[int, int]:
    """按 token 估算拆分 (模型 pin, 系统 pin)。

    只读 ``PinnedItem.content``：file_path pin 的最新内容要等重注入时才刷新，这里刻意
    不做文件 I/O——账本用于趋势判断，缓存值足够，而且避免每次采样都扫盘。
    """
    model_tokens = 0
    system_tokens = 0
    for item in short_term.pinned_items:
        tokens = estimate_text_tokens(item.content)
        if item.system_managed:
            system_tokens += tokens
        else:
            model_tokens += tokens
    return model_tokens, system_tokens


@dataclass(frozen=True)
class ContextBudgetSnapshot:
    """一次采样得到的上下文预算快照（全部为 token 估算值）。

    ``bands`` 记录采样时生效的阈值，使存档/管理端读到的档位与当时的策略一致。
    """

    window_tokens: int
    spine_tokens: int
    model_pin_tokens: int
    system_pin_tokens: int
    prefix_tokens: int
    thinking_tokens: int
    tail_tokens: int
    bands: tuple[float, ...] = BUDGET_BANDS

    @property
    def pin_tokens(self) -> int:
        return self.model_pin_tokens + self.system_pin_tokens

    @property
    def irreducible_tokens(self) -> int:
        """压缩砍不掉的部分：脊柱 + 全部 pin + 提示词与工具定义。"""
        return self.spine_tokens + self.pin_tokens + self.prefix_tokens

    @property
    def ratio(self) -> float:
        if self.window_tokens <= 0:
            return 0.0
        return self.irreducible_tokens / self.window_tokens

    def band(self) -> int:
        return band_for(self.ratio, self.bands)

    def to_dict(self) -> dict[str, int | float]:
        return {
            "window_tokens": self.window_tokens,
            "spine_tokens": self.spine_tokens,
            "model_pin_tokens": self.model_pin_tokens,
            "system_pin_tokens": self.system_pin_tokens,
            "prefix_tokens": self.prefix_tokens,
            "thinking_tokens": self.thinking_tokens,
            "tail_tokens": self.tail_tokens,
            "pin_tokens": self.pin_tokens,
            "irreducible_tokens": self.irreducible_tokens,
            "ratio": round(self.ratio, 4),
            "band": self.band(),
        }


class ContextBudgetWatcher:
    """按压缩推进计数的提醒节流器：决定"此刻该不该提醒"。

    规则：``band`` 越高允许的次数越多（1/2/3），总数不超过 ``max_warnings``，相邻两次
    至少间隔 ``min_gap_cycles``；下一次压缩（``compress_generation`` 变化）或比例回落到
    安全区时清零，这样模型一旦精简下来，之后再涨回去仍会被提醒。
    """

    def __init__(
        self,
        *,
        bands: tuple[float, ...] = BUDGET_BANDS,
        max_warnings: int = MAX_BUDGET_WARNINGS,
        min_gap_cycles: int = MIN_BUDGET_WARNING_GAP_CYCLES,
    ) -> None:
        if not bands:
            raise ValueError("bands must not be empty")
        if max_warnings < 1:
            raise ValueError("max_warnings must be positive")
        self._bands = tuple(bands)
        self._max_warnings = int(max_warnings)
        self._min_gap = max(0, int(min_gap_cycles))
        self._generation: int | None = None
        self._warned = 0
        self._last_cycle: int | None = None

    @property
    def warned(self) -> int:
        """自上次压缩以来已经提醒过的次数。"""
        return self._warned

    @property
    def bands(self) -> tuple[float, ...]:
        """当前生效的档位阈值（供采样快照记录同一套策略）。"""
        return self._bands

    def observe(
        self, snapshot: ContextBudgetSnapshot, *, cycle: int, generation: int
    ) -> int | None:
        """记录一次采样；返回提醒后到下次压缩前剩余的次数，``None`` 表示这次不提醒。"""
        if generation != self._generation:
            self._generation = generation
            self._warned = 0
            self._last_cycle = None

        band = band_for(snapshot.ratio, self._bands)
        if band == 0:
            # 回落到安全区：清零，让下次逼近重新计数。
            self._warned = 0
            self._last_cycle = None
            return None

        if self._warned >= self._max_warnings:
            return None
        if self._warned >= min(band, self._max_warnings):
            return None
        if self._last_cycle is not None and cycle - self._last_cycle < self._min_gap:
            return None

        self._warned += 1
        self._last_cycle = cycle
        return self._max_warnings - self._warned
