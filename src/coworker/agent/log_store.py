from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

# 进入 digest 的条目类型（对话实质）；system_prompt / auto_recall / palace_injection
# 等体量大或非叙事的条目跳过，避免摘要被噪声淹没。
_DIGEST_TYPES = {
    "message_in",
    "message_tick",
    "llm_response",
    "tool_call",
    "tool_result",
    "task_reminder",
    "subconscious_done",
}

# 单条 digest 文本的截断长度，防止单个超长 tool_result 撑爆摘要输入。
_MAX_ENTRY_CHARS = 2000


@dataclass
class ShardInfo:
    path: Path
    ts_min: str
    ts_max: str
    seq_min: int
    seq_max: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path.name,
            "ts_min": self.ts_min,
            "ts_max": self.ts_max,
            "seq_min": self.seq_min,
            "seq_max": self.seq_max,
        }


@dataclass(frozen=True)
class LogPageCursor:
    """A resumable byte cursor for reverse chronological log browsing.

    ``offset`` is the byte immediately before the next (older) JSONL record. It
    makes consecutive pages from a large shard proportional to the page size
    instead of repeatedly scanning that shard from EOF. ``before_seq`` is a
    stable fallback when a live shard rotates between requests and changes its
    filename.
    """

    path: str
    offset: int
    before_seq: int | None = None


@dataclass
class LogPage:
    """One reverse-chronological, bounded scan of the interaction history."""

    entries: list[dict[str, Any]]
    cursor: LogPageCursor | None
    has_more: bool
    scanned_bytes: int


class LogStore:
    """原始交互日志的只读寻址层，对记忆块树屏蔽物理分片细节。

    地址用 seq 区间（稳定主键）或 ts 区间（人读、ISO 字典序即时序）。日志后续会
    分裂/轮转成多个 ``interactions*.jsonl`` 分片；本层按各分片的 seq/ts 范围只打开
    覆盖目标区间的分片读取。分片缺失/已归档时优雅降级（返回部分结果或 None），
    调用方据此回退到节点自存的摘要。

    写入端会按大小轮转为 ``interactions-000001.jsonl`` 等已完成分片，
    ``interactions.jsonl`` 始终是当前可追加分片；本层无需随轮转改动，树的指针也无需改动。
    """

    def __init__(self, logs_dir: str | Path, log_basename: str = "interactions") -> None:
        self._dir = Path(logs_dir)
        self._basename = log_basename
        self._manifest_path = self._dir / "manifest.json"
        # (path, size, mtime) -> ShardInfo 缓存，避免重复扫描未变化的分片边界。
        self._scan_cache: dict[tuple[str, int, float], ShardInfo] = {}
        # 上次落盘的 manifest 签名，避免在读路径（每次 manifest()）重复写同样内容。
        self._last_manifest_sig: tuple[tuple[str, int, int, str, str], ...] | None = None

    # ---- 分片发现与边界扫描 ----------------------------------------------

    def _shard_paths(self) -> list[Path]:
        # 只匹配当前 interactions.jsonl 和编号归档 interactions-000001.jsonl，
        # 避免把用户手工放入的 interactions-backup.jsonl 一类文件误并入历史。
        active = self._dir / f"{self._basename}.jsonl"
        prefix = f"{self._basename}-"
        paths = [active] if active.is_file() else []
        for candidate in self._dir.glob(f"{prefix}*.jsonl"):
            suffix = candidate.stem[len(prefix) :]
            if candidate.is_file() and suffix.isdecimal():
                paths.append(candidate)
        return sorted(paths)

    def manifest(self) -> list[ShardInfo]:
        """当前各分片的 seq/ts 范围，按 seq_min 升序。即时扫描磁盘为准。"""
        shards: list[ShardInfo] = []
        for p in self._shard_paths():
            info = self._scan_shard(p)
            if info is not None:
                shards.append(info)
        shards.sort(key=lambda s: s.seq_min)
        self._persist_manifest(shards)
        return shards

    def _scan_shard(self, path: Path) -> ShardInfo | None:
        try:
            st = path.stat()
        except OSError:
            return None
        key = (str(path), st.st_size, st.st_mtime)
        cached = self._scan_cache.get(key)
        if cached is not None:
            return cached
        first = self._parse_line(self._read_first_line(path))
        last = self._parse_line(self._read_last_line(path))
        if first is None or last is None:
            return None
        info = ShardInfo(
            path=path,
            ts_min=str(first.get("ts", "")),
            ts_max=str(last.get("ts", "")),
            seq_min=int(first.get("seq", 0)),
            seq_max=int(last.get("seq", 0)),
        )
        self._scan_cache[key] = info
        return info

    def _persist_manifest(self, shards: list[ShardInfo]) -> None:
        # 持久化仅供观测 / 后续轮转使用；读取始终以磁盘扫描为真。
        # 只在内容变化时写盘：manifest() 在读路径（recall 下钻）被频繁调用，避免写放大。
        sig = tuple((s.path.name, s.seq_min, s.seq_max, s.ts_min, s.ts_max) for s in shards)
        if sig == self._last_manifest_sig:
            return
        self._last_manifest_sig = sig
        try:
            self._manifest_path.write_text(
                json.dumps([s.to_dict() for s in shards], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"Failed to persist log manifest to {self._manifest_path}: {e}")

    @staticmethod
    def _parse_line(line: str | None) -> dict[str, Any] | None:
        if not line:
            return None
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _read_first_line(path: Path) -> str | None:
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        return line
        except OSError:
            return None
        return None

    @staticmethod
    def _read_last_line(path: Path) -> str | None:
        """从文件尾部反向 seek 读最后一条非空行，避免整文件读入。"""
        try:
            with path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return None
                block = 4096
                data = b""
                pos = size
                while pos > 0:
                    step = min(block, pos)
                    pos -= step
                    f.seek(pos)
                    data = f.read(step) + data
                    # 只按 \n 切（不用 splitlines：它还会在 \r\v\f\x1c-\x1e 处误切，
                    # 把含这些字节的 JSON 行截成碎片→解析失败→整个分片被丢出 manifest）。
                    lines = data.split(b"\n")
                    # 至少有一条完整行（行首已被包含）时，取最后一条非空
                    if pos == 0 or len(lines) > 1:
                        for ln in reversed(lines):
                            if ln.strip():
                                return ln.decode("utf-8", errors="replace")
                        return None
        except OSError:
            return None
        return None

    # ---- 区间读取 --------------------------------------------------------

    def read_seq_range(self, seq_start: int, seq_end: int) -> tuple[list[dict[str, Any]], bool]:
        """返回 [seq_start, seq_end] 内的条目（按 seq 升序）与 complete 标志。

        complete=False 表示有覆盖该区间的分片缺失/不可读（已归档），结果为部分。
        """
        shards = self.manifest()
        covering = [s for s in shards if s.seq_max >= seq_start and s.seq_min <= seq_end]
        entries: list[dict[str, Any]] = []
        complete = True
        for s in covering:
            rows = self._read_shard_filtered(
                s.path, lambda e: seq_start <= int(e.get("seq", -1)) <= seq_end
            )
            if rows is None:
                complete = False
                continue
            entries.extend(rows)
        entries.sort(key=lambda e: int(e.get("seq", 0)))
        return entries, complete

    def read_time_range(
        self, t_start: datetime, t_end: datetime
    ) -> tuple[list[dict[str, Any]], bool]:
        """同 read_seq_range，但按 ts 过滤（ISO 字典序即时序）。"""
        t0, t1 = t_start.isoformat(), t_end.isoformat()
        shards = self.manifest()
        # ts 非单调，分片范围只作粗筛；最终按字符串区间精筛。
        covering = [s for s in shards if not (s.ts_max < t0 or s.ts_min > t1)] or shards
        entries: list[dict[str, Any]] = []
        complete = True
        for s in covering:
            rows = self._read_shard_filtered(s.path, lambda e: t0 <= str(e.get("ts", "")) <= t1)
            if rows is None:
                complete = False
                continue
            entries.extend(rows)
        entries.sort(key=lambda e: (str(e.get("ts", "")), int(e.get("seq", 0))))
        return entries, complete

    @staticmethod
    def _read_shard_filtered(
        path: Path, keep: Callable[[dict[str, Any]], bool]
    ) -> list[dict[str, Any]] | None:
        try:
            rows: list[dict[str, Any]] = []
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if keep(e):
                        rows.append(e)
            return rows
        except OSError:
            return None

    # ---- digest（喂给 brain.summarize 的紧凑转写）------------------------

    def digest_seq_range(self, seq_start: int, seq_end: int) -> str | None:
        entries, _complete = self.read_seq_range(seq_start, seq_end)
        return self._digest(entries)

    def recall_time_range(self, t_start: datetime, t_end: datetime) -> tuple[str | None, bool]:
        """读取时间窗的 digest 文本 + complete 标志，供 ``query_memory(start=..., end=...)`` 日志回退：
        区分「窗口空但日志可达」与「分片缺失/已归档不可达」，给出准确提示。"""
        entries, complete = self.read_time_range(t_start, t_end)
        return self._digest(entries), complete

    def digest_entries(self, entries: list[dict[str, Any]]) -> str | None:
        return self._digest(entries)

    # ---- 历史回溯支持 ----------------------------------------------------

    def read_all(self) -> tuple[list[dict[str, Any]], bool]:
        """读取所有分片的全部条目（按 ts,seq 升序）。供一次性历史回溯遍历。

        注意：会把全史一次性载入内存。分片限制单个文件大小，但不会限制总历史；
        总量很大时，调用方应优先使用流式接口或将这里改为按分片流式分块。
        """
        shards = self.manifest()
        entries: list[dict[str, Any]] = []
        complete = True
        for s in shards:
            rows = self._read_shard_filtered(s.path, lambda e: True)
            if rows is None:
                complete = False
                continue
            entries.extend(rows)
        entries.sort(key=lambda e: (str(e.get("ts", "")), int(e.get("seq", 0))))
        return entries, complete

    def iter_all_entries(self) -> Iterator[dict[str, Any]]:
        """Stream all entries shard-by-shard without materializing the full log.

        Order is physical shard order rather than globally sorted. This is intended for
        commutative consumers such as usage counters, where retaining all entries in
        memory would be wasteful.
        """
        for shard in self.manifest():
            try:
                with shard.path.open("r", encoding="utf-8") as f:
                    for line in f:
                        entry = self._parse_line(line.strip())
                        if entry is not None:
                            yield entry
            except OSError as e:
                logger.warning(f"Failed to stream log shard {shard.path}: {e}")

    def iter_entries_after(self, seq: int) -> Iterator[dict[str, Any]]:
        """Stream entries after ``seq`` from shards that can contain newer entries."""
        for shard in [shard for shard in self.manifest() if shard.seq_max > seq]:
            for entry in self._iter_shard_from(shard.path):
                try:
                    entry_seq = int(entry.get("seq", -1))
                except (TypeError, ValueError):
                    continue
                if entry_seq > seq:
                    yield entry

    @staticmethod
    def _iter_shard_from(path: Path) -> Iterator[dict[str, Any]]:
        try:
            with path.open("rb") as f:
                for raw in f:
                    line = raw.decode("utf-8", errors="replace").strip()
                    entry = LogStore._parse_line(line)
                    if entry is not None:
                        yield entry
        except OSError as e:
            logger.warning(f"Failed to stream log shard {path}: {e}")

    def read_tail(self, max_lines: int) -> tuple[list[dict[str, Any]], bool]:
        """只读取原始日志尾部最多 ``max_lines`` 条 JSONL 记录（按 ts,seq 升序）。

        这是运行日志历史回放的轻量路径：从最新分片开始按文件尾部反向读，凑够行数就停止，
        不扫描全史，也不按时间窗打开所有覆盖分片。
        """
        if max_lines <= 0:
            return [], True
        shards = sorted(self.manifest(), key=lambda s: s.seq_max, reverse=True)
        entries: list[dict[str, Any]] = []
        complete = True
        remaining = max_lines
        for s in shards:
            rows = self._read_shard_tail(s.path, remaining)
            if rows is None:
                complete = False
                continue
            entries.extend(rows)
            remaining -= len(rows)
            if remaining <= 0:
                break
        entries.sort(key=lambda e: (str(e.get("ts", "")), int(e.get("seq", 0))))
        return entries, complete

    def read_recent_days(
        self, days: int | float, now: datetime | None = None
    ) -> tuple[list[dict[str, Any]], bool]:
        """读取最近 ``days`` 天的条目（按 ts,seq 升序）。

        与 ``read_all`` 不同，这里先按分片时间边界粗筛，再在分片内流式过滤，只把窗口内
        的行放进内存。用于运行日志历史回放等只需要近期上下文的读路径。
        """
        if days <= 0:
            return [], True
        end = now or datetime.now()
        start = end - timedelta(days=days)
        return self.read_time_range(start, end)

    # ---- 管理端全史浏览 -------------------------------------------------

    def read_history_page(
        self,
        *,
        limit: int,
        cursor: LogPageCursor | None = None,
        match: Callable[[dict[str, Any]], bool] | None = None,
        max_scan_bytes: int = 2 * 1024 * 1024,
        seq_start: int | None = None,
        seq_end: int | None = None,
    ) -> LogPage:
        """Read one older-to-newer browsing page without materializing history.

        Results are returned newest-first. The optional cursor resumes at the
        byte immediately before the oldest record consumed by the previous
        page. A request is additionally bounded by ``max_scan_bytes`` so a rare
        full-text search never turns one HTTP request into a full-history scan;
        callers can keep following ``cursor`` until ``has_more`` is false.

        ``seq_start`` / ``seq_end`` select an inclusive sequence interval.  The
        manifest lets the reader skip unrelated shards outright; when the upper
        bound falls inside a shard, a logarithmic byte seek avoids walking from
        that shard's newest record down to the requested sequence.
        """
        if limit <= 0:
            return LogPage(entries=[], cursor=None, has_more=False, scanned_bytes=0)
        if seq_start is not None and seq_start < 0:
            raise ValueError("seq_start must be non-negative")
        if seq_end is not None and seq_end < 0:
            raise ValueError("seq_end must be non-negative")
        if seq_start is not None and seq_end is not None and seq_start > seq_end:
            raise ValueError("seq_start must not exceed seq_end")

        shards = sorted(self.manifest(), key=lambda shard: shard.seq_max, reverse=True)
        if seq_start is not None:
            shards = [shard for shard in shards if shard.seq_max >= seq_start]
        if seq_end is not None:
            shards = [shard for shard in shards if shard.seq_min <= seq_end]
        if not shards:
            return LogPage(entries=[], cursor=None, has_more=False, scanned_bytes=0)

        start_index, start_offset, before_seq = self._history_cursor_start(shards, cursor)
        if start_index is None:
            return LogPage(entries=[], cursor=None, has_more=False, scanned_bytes=0)
        if cursor is None and seq_end is not None:
            start_shard = shards[start_index]
            if start_shard.seq_min <= seq_end < start_shard.seq_max:
                direct_offset = self._history_end_offset_for_seq(start_shard.path, seq_end)
                if direct_offset is not None:
                    start_offset = direct_offset

        entries: list[dict[str, Any]] = []
        scanned_bytes = 0
        scan_budget = max(1, max_scan_bytes)

        for index in range(start_index, len(shards)):
            shard = shards[index]
            remaining_limit = limit - len(entries)
            remaining_budget = max(1, scan_budget - scanned_bytes)
            shard_entries, next_offset, consumed_seq, scanned, exhausted = (
                self._read_shard_reverse_page(
                    shard.path,
                    end_offset=start_offset if index == start_index else None,
                    limit=remaining_limit,
                    before_seq=before_seq,
                    match=match,
                    max_scan_bytes=remaining_budget,
                    min_seq=seq_start,
                    max_seq=seq_end,
                )
            )
            entries.extend(shard_entries)
            scanned_bytes += scanned
            if consumed_seq is not None:
                before_seq = consumed_seq

            next_cursor: LogPageCursor | None
            stopped = len(entries) >= limit or (scanned_bytes >= scan_budget and not exhausted)
            if stopped:
                if not exhausted and next_offset is not None:
                    next_cursor = LogPageCursor(shard.path.name, next_offset, before_seq)
                    return LogPage(entries, next_cursor, True, scanned_bytes)
                next_cursor = self._history_cursor_for_next_shard(
                    shards, index + 1, before_seq
                )
                return LogPage(entries, next_cursor, next_cursor is not None, scanned_bytes)

            if scanned_bytes >= scan_budget and index + 1 < len(shards):
                next_cursor = self._history_cursor_for_next_shard(shards, index + 1, before_seq)
                return LogPage(entries, next_cursor, next_cursor is not None, scanned_bytes)

        return LogPage(entries, None, False, scanned_bytes)

    @staticmethod
    def _history_cursor_for_next_shard(
        shards: list[ShardInfo], index: int, before_seq: int | None
    ) -> LogPageCursor | None:
        if index >= len(shards):
            return None
        path = shards[index].path
        try:
            offset = path.stat().st_size
        except OSError:
            return None
        return LogPageCursor(path.name, offset, before_seq)

    @staticmethod
    def _history_cursor_start(
        shards: list[ShardInfo], cursor: LogPageCursor | None
    ) -> tuple[int | None, int | None, int | None]:
        if cursor is None:
            return 0, None, None

        exact_index = next(
            (index for index, shard in enumerate(shards) if shard.path.name == cursor.path),
            None,
        )
        if exact_index is not None and (
            cursor.before_seq is None or cursor.before_seq >= shards[exact_index].seq_min
        ):
            try:
                size = shards[exact_index].path.stat().st_size
            except OSError:
                size = 0
            offset = cursor.offset if 0 <= cursor.offset <= size else None
            return exact_index, offset, cursor.before_seq

        if cursor.before_seq is None:
            return None, None, None

        # A live interactions.jsonl can be renamed by size rotation between two
        # clicks. The same bytes normally exist in its new archive shard, so
        # retain the byte offset when its sequence range still contains the
        # cursor boundary. Otherwise fall back to the first older shard.
        for index, shard in enumerate(shards):
            if shard.seq_min <= cursor.before_seq <= shard.seq_max:
                try:
                    size = shard.path.stat().st_size
                except OSError:
                    size = 0
                offset = cursor.offset if 0 <= cursor.offset <= size else None
                return index, offset, cursor.before_seq
        for index, shard in enumerate(shards):
            if shard.seq_max < cursor.before_seq:
                return index, None, cursor.before_seq
        return None, None, cursor.before_seq

    @staticmethod
    def _line_start_at_or_before(fh: Any, position: int) -> int:
        """Return the start of the JSONL line containing ``position``.

        This is used only for direct sequence jumps.  It seeks backward in
        small blocks instead of reading the file prefix, so finding a line
        boundary remains cheap in a multi-megabyte shard.
        """
        end = max(0, position)
        block_size = 4 * 1024
        while end > 0:
            start = max(0, end - block_size)
            fh.seek(start)
            newline = fh.read(end - start).rfind(b"\n")
            if newline >= 0:
                return start + newline + 1
            end = start
        return 0

    @classmethod
    def _history_end_offset_for_seq(cls, path: Path, seq_end: int) -> int | None:
        """Find the byte immediately before the first record after ``seq_end``.

        InteractionLogger writes monotonically increasing sequence numbers.  A
        binary search over complete JSONL lines therefore positions an initial
        range request near its upper sequence boundary without loading a shard.
        Returning ``None`` keeps the regular bounded tail reader as a safe
        fallback for malformed legacy data.
        """
        try:
            with path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                lower = 0
                upper = size
                result = size
                while lower < upper:
                    probe = (lower + upper) // 2
                    line_start = cls._line_start_at_or_before(fh, probe)
                    fh.seek(line_start)
                    raw_line = fh.readline()
                    if not raw_line:
                        return None
                    entry = cls._parse_line(raw_line.decode("utf-8", errors="replace").strip())
                    if entry is None:
                        return None
                    try:
                        entry_seq = int(entry.get("seq", -1))
                    except (TypeError, ValueError, OverflowError):
                        return None
                    if entry_seq < 0:
                        return None
                    line_end = fh.tell()
                    if line_end <= lower:
                        return None
                    if entry_seq <= seq_end:
                        lower = line_end
                    else:
                        result = line_start
                        upper = line_start
                return result
        except OSError:
            return None

    @classmethod
    def _read_shard_reverse_page(
        cls,
        path: Path,
        *,
        end_offset: int | None,
        limit: int,
        before_seq: int | None,
        match: Callable[[dict[str, Any]], bool] | None,
        max_scan_bytes: int,
        min_seq: int | None,
        max_seq: int | None,
    ) -> tuple[list[dict[str, Any]], int | None, int | None, int, bool]:
        """Read a bounded reverse page from one JSONL shard.

        The implementation reads fixed-size byte blocks from the tail and keeps
        a partial leading line across blocks. It therefore never calls
        ``read_text`` on a potentially large shard, while the returned byte
        offset remains safe even when a record crosses a block boundary.
        """
        try:
            with path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                end = size if end_offset is None else min(max(0, end_offset), size)
                if end <= 0:
                    return [], None, before_seq, 0, True

                entries: list[dict[str, Any]] = []
                scanned_bytes = 0
                consumed_seq = before_seq
                last_consumed_start: int | None = None
                carry = b""
                block_size = 64 * 1024

                while end > 0:
                    step = min(block_size, end)
                    start = end - step
                    fh.seek(start)
                    data = fh.read(step) + carry
                    scanned_bytes += step
                    parts = data.split(b"\n")
                    positions: list[tuple[int, bytes]] = []
                    position = start
                    for part in parts:
                        positions.append((position, part))
                        position += len(part) + 1

                    first_complete = 0 if start == 0 else 1
                    carry = b"" if start == 0 else parts[0]
                    for line_start, raw_line in reversed(positions[first_complete:]):
                        # The trailing element of a newline-terminated JSONL
                        # file is empty; it still advances the cursor but is
                        # not a record.
                        last_consumed_start = line_start
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        entry = cls._parse_line(line)
                        if entry is None:
                            continue
                        try:
                            entry_seq = int(entry.get("seq", -1))
                        except (TypeError, ValueError, OverflowError):
                            entry_seq = -1
                        if entry_seq >= 0 and (
                            consumed_seq is None or entry_seq < consumed_seq
                        ):
                            consumed_seq = entry_seq
                        if before_seq is not None and entry_seq >= before_seq:
                            continue
                        if min_seq is not None and 0 <= entry_seq < min_seq:
                            return entries, None, consumed_seq, scanned_bytes, True
                        if max_seq is not None and entry_seq > max_seq:
                            continue
                        if match is not None and not match(entry):
                            continue
                        entries.append(entry)
                        if min_seq is not None and entry_seq == min_seq:
                            return entries, None, consumed_seq, scanned_bytes, True
                        if len(entries) >= limit:
                            return entries, last_consumed_start, consumed_seq, scanned_bytes, False

                    end = start
                    if scanned_bytes >= max_scan_bytes and end > 0 and last_consumed_start is not None:
                        return entries, last_consumed_start, consumed_seq, scanned_bytes, False
        except OSError:
            return [], None, before_seq, 0, True

        return entries, None, consumed_seq, scanned_bytes, True

    @staticmethod
    def _read_shard_tail(path: Path, max_lines: int) -> list[dict[str, Any]] | None:
        try:
            raw_lines = LogStore._read_last_lines(path, max_lines)
        except OSError:
            return None
        rows: list[dict[str, Any]] = []
        for line in raw_lines:
            e = LogStore._parse_line(line)
            if e is not None:
                rows.append(e)
        return rows

    @staticmethod
    def _read_last_lines(path: Path, max_lines: int) -> list[str]:
        """从文件尾部反向读取最后 N 条非空行，避免把整个 JSONL 文件读入内存。"""
        if max_lines <= 0:
            return []
        with path.open("rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            if pos == 0:
                return []
            block = 64 * 1024
            partial = b""
            lines: list[bytes] = []
            while pos > 0 and len(lines) < max_lines:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + partial
                parts = data.split(b"\n")
                if pos > 0:
                    partial = parts[0]
                    complete = parts[1:]
                else:
                    partial = b""
                    complete = parts
                lines = [ln for ln in complete if ln.strip()] + lines
                if len(lines) > max_lines:
                    lines = lines[-max_lines:]
            return [ln.decode("utf-8", errors="replace") for ln in lines[-max_lines:]]

    def backfill_chunks(
        self,
        before: datetime | None = None,
        target_chars: int = 4000,
        max_chunks: int = 64,
    ) -> list[list[dict[str, Any]]]:
        """把（``before`` 之前的）对话类历史条目按内容大小切成时序块，供回溯逐块摘要成树叶。

        每块约 ``target_chars`` 字符；若总量大到会超过 ``max_chunks`` 块，则自动放大目标
        块尺寸把总块数压回 ~``max_chunks``（封顶一次性回溯的 LLM 调用数）。``before`` 用
        primary 最旧消息的时间，避免脊柱与仍在 primary 里的近期内容重叠。
        """
        entries, _ = self.read_all()
        cutoff = before.isoformat() if before is not None else None
        conv = [
            e for e in entries
            if e.get("type") in _DIGEST_TYPES
            and (cutoff is None or str(e.get("ts", "")) < cutoff)
        ]
        if not conv:
            return []
        sizes = [len(self._entry_to_text(e)) or 1 for e in conv]
        target = max(target_chars, sum(sizes) // max(1, max_chunks) + 1)
        chunks: list[list[dict[str, Any]]] = []
        cur: list[dict[str, Any]] = []
        acc = 0
        for e, sz in zip(conv, sizes):
            cur.append(e)
            acc += sz
            if acc >= target:
                chunks.append(cur)
                cur, acc = [], 0
        if cur:
            chunks.append(cur)
        return chunks

    def _digest(self, entries: list[dict[str, Any]]) -> str | None:
        lines = [self._entry_to_text(e) for e in entries if e.get("type") in _DIGEST_TYPES]
        lines = [ln for ln in lines if ln]
        if not lines:
            return None
        return "\n".join(lines)

    @staticmethod
    def _truncate(s: str) -> str:
        s = s if isinstance(s, str) else str(s)
        return s if len(s) <= _MAX_ENTRY_CHARS else s[:_MAX_ENTRY_CHARS] + "…(截断)"

    def _entry_to_text(self, e: dict[str, Any]) -> str:
        t = e.get("type")
        if t == "message_in":
            return f"[{e.get('participant_id', '?')}] {self._truncate(e.get('content', ''))}"
        if t == "message_tick":
            return f"[tick] {self._truncate(e.get('content', ''))}"
        if t == "llm_response":
            parts = []
            content = e.get("content") or ""
            if content:
                parts.append(f"[助手] {self._truncate(content)}")
            for tc in e.get("tool_calls", []) or []:
                args = self._truncate(json.dumps(tc.get("arguments", {}), ensure_ascii=False))
                parts.append(f"  →调用 {tc.get('name', '?')}({args})")
            return "\n".join(parts)
        if t == "tool_call":
            return f"  →调用 {e.get('name', '?')}"
        if t == "tool_result":
            tag = "错误" if e.get("is_error") else "结果"
            return f"  ←{e.get('name', '?')} {tag}: {self._truncate(e.get('content', ''))}"
        if t == "task_reminder":
            return f"[任务提醒] {len(e.get('tasks', []))} 个任务"
        if t == "subconscious_done":
            return f"[潜意识·{e.get('mode', '?')}] {self._truncate(e.get('result', ''))}"
        return ""
