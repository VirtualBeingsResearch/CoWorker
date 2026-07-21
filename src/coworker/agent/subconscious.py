from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from coworker.agent.bubble_loop import BubbleMiniLoop, _bubble_base_intercepts
from coworker.agent.subconscious_mode import SubconsciousMode, SubconsciousModeLoader
from coworker.i18n import bind_locale, tr

if TYPE_CHECKING:
    from coworker.agent.bubble import Bubble, BubbleStore
    from coworker.agent.inbox_watcher import InboxWatcher
    from coworker.agent.interaction_log import InteractionLogger
    from coworker.agent.usage_stats import UsageStatsCollector
    from coworker.brain.brain import Brain
    from coworker.core.config import Config
    from coworker.core.types import Message
    from coworker.memory.long_term import LongTermMemory
    from coworker.memory.short_term import ShortTermMemory
    from coworker.palaces.loader import Palace, PalaceLoader
    from coworker.prompts.system_prompt import SystemPromptBuilder
    from coworker.tools.reasoning_tools import TaskStore
    from coworker.tools.registry import ToolRegistry


# Built-in intercepts applied to every subconscious bubble, regardless of mode.
# Modes with empty extra_intercepts (the default) inherit the full set below.
def _subconscious_extra_intercepts() -> dict[str, str]:
    return {
        "switch_model": tr("subconscious.intercept_switch_model"),
        "communicate": tr("subconscious.intercept_communicate"),
        "list_ws_connections": tr("subconscious.intercept_connections"),
        "set_alarm": tr("subconscious.intercept_set_alarm"),
        "cancel_alarm": tr("subconscious.intercept_cancel_alarm"),
        "list_alarms": tr("subconscious.intercept_list_alarms"),
        "bubble_spawn": tr("subconscious.intercept_spawn"),
        "bubble_cancel": tr("subconscious.intercept_bubbles"),
        "bubble_list": tr("subconscious.intercept_bubbles"),
        "bubble_check": tr("subconscious.intercept_bubbles"),
        "write_file": tr("subconscious.intercept_write"),
        "execute_code": tr("subconscious.intercept_code"),
        "kill_code_job": tr("subconscious.intercept_jobs"),
    }


# Backward-compatible import for callers/tests; runtime paths rebuild it in the active locale.
_SUBCONSCIOUS_EXTRA_INTERCEPTS = _subconscious_extra_intercepts()

# How often to reload mode files from disk (seconds). Large enough that test-set
# in-memory modes are never inadvertently cleared; small enough that production
# edits to MODE.md take effect within a minute without restart.
_MODE_RELOAD_INTERVAL = 60.0


def _mode_hash(mode: SubconsciousMode) -> str:
    """Stable content fingerprint for a mode — body + all scheduling/behavior/lifecycle fields."""
    parts = "|".join(
        [
            mode.body,
            str(mode.trigger),
            str(mode.context_builder),
            str(mode.enabled),
            str(mode.every_n_cycles),
            str(mode.every_seconds),
            str(mode.every_n_tool_calls),
            str(mode.cold_floor_seconds),
            str(mode.use_threshold),
            str(mode.min_interval_seconds),
            str(mode.max_cycles),
            str(mode.grants_task_store),
            str(mode.inject_skill_anomalies),
            str(mode.inject_telemetry),
            str(mode.purpose),
            str(mode.retire_after),
            str(mode.protected),
        ]
    )
    return hashlib.md5(parts.encode()).hexdigest()[:12]


class SubconsciousMiniLoop(BubbleMiniLoop):
    """Subconscious variant: silent merge (no inbox push) + mode-specific identity."""

    def __init__(self, mode: str, identity_body: str, intercepts: dict[str, str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._mode = mode
        self._identity_body = identity_body
        self._intercepts = intercepts

    def _tool_intercepts(self) -> dict[str, str]:
        base = {**_bubble_base_intercepts(), **self._intercepts}
        reminder = tr(
            "subconscious.identity_reminder",
            id=self._bubble.id,
            mode=self._mode,
        )
        return {name: reason + reminder for name, reason in base.items()}

    def _log_filename(self, bubble: Bubble) -> str:
        return f"{bubble.id}_{self._mode}.jsonl"

    def _build_identity_content(self, bubble: Bubble) -> str:
        return (
            self._identity_body.replace("{bubble_id}", bubble.id)
            .replace("{goal}", bubble.goal)
            .replace("{max_cycles}", str(bubble.max_cycles))
        )

    async def _auto_merge(self) -> None:
        self._store.mark_done(self._bubble)


class SubconsciousScheduler:
    def __init__(
        self,
        cfg: Config,
        bubble_store: BubbleStore,
        brain: Brain,
        tool_registry: ToolRegistry,
        prompt_builder: SystemPromptBuilder,
        short_term: ShortTermMemory,
        inbox: InboxWatcher,
        logs_dir: str,
        interaction_log: InteractionLogger | None = None,
        usage_stats: UsageStatsCollector | None = None,
        state_path: Path | None = None,
        task_store: TaskStore | None = None,
        palace_loader: PalaceLoader | None = None,
        long_term: LongTermMemory | None = None,
        mode_loader: SubconsciousModeLoader | None = None,
    ) -> None:
        self._cfg = cfg
        self._bubble_store = bubble_store
        self._brain = brain
        self._tools = tool_registry
        self._prompt_builder = prompt_builder
        self._short_term = short_term
        self._inbox = inbox
        self._logs_dir = logs_dir
        self._ilog = interaction_log
        self._usage_stats = usage_stats
        self._state_path = state_path
        self._task_store = task_store
        self._palace_loader = palace_loader
        self._long_term = long_term
        self._mode_loader = mode_loader or SubconsciousModeLoader("/nonexistent")

        _now = time.monotonic()

        # Per-mode scheduling state (replaces 12 fixed _last_<mode>_* attributes).
        self._last_cycle: dict[str, int] = {}
        self._last_time: dict[str, float] = {}
        self._last_tool_calls: dict[str, int] = {}
        self._active_by_mode: dict[str, str | None] = {}

        # Lightweight telemetry per mode.
        self._mode_run_count: dict[str, int] = {}
        self._mode_last_run_wall: dict[str, float | None] = {}
        self._mode_recent_results: dict[str, list[str]] = {}

        # Change tracking: fingerprint each mode's content so reloads can detect edits.
        self._mode_content_hash: dict[str, str] = {}
        self._mode_last_changed_wall: dict[str, float | None] = {}

        # Garden-specific state (unchanged).
        self._total_tool_calls: int = 0
        self._garden_index: int = 0
        self._palace_use_counts: dict[str, int] = {}
        self._palace_last_garden_time: dict[str, float] = {}
        self._counted_bubble_ids: set[str] = set()

        # Throttle for reloading mode files; initialized to now so first reload
        # is deferred by _MODE_RELOAD_INTERVAL seconds.
        self._last_mode_reload: float = _now

        # Initialize per-mode dicts from currently loaded modes.
        for mode in self._mode_loader.list_all():
            self._last_cycle[mode.name] = 0
            self._last_time[mode.name] = _now
            self._last_tool_calls[mode.name] = 0
            self._active_by_mode[mode.name] = None

        self._load_state()

        # Fill gaps for any modes not covered by saved state.
        for mode in self._mode_loader.list_all():
            self._last_cycle.setdefault(mode.name, 0)
            self._last_time.setdefault(mode.name, _now)
            self._last_tool_calls.setdefault(mode.name, 0)
            self._active_by_mode.setdefault(mode.name, None)
            # Compute initial hash for modes without persisted fingerprint.
            # We don't record a change timestamp here — the system was just started,
            # so we have no baseline to compare against.
            if mode.name not in self._mode_content_hash:
                self._mode_content_hash[mode.name] = _mode_hash(mode)

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def notify_cycle_complete(
        self,
        cycle_count: int,
        short_term_snapshot: list[Message],
        tool_calls_this_cycle: int = 0,
    ) -> None:
        now = time.monotonic()
        self._total_tool_calls += tool_calls_this_cycle
        self._maybe_reload_modes(now)
        self._tally_palace_usage()
        for mode in self._mode_loader.list_all():
            if self._has_active_mode(mode.name):
                continue
            if not self._due(mode, cycle_count, now):
                continue
            await self._dispatch(mode, cycle_count, now, short_term_snapshot)
        self.save_state()

    async def notify_pre_compress(self, short_term_snapshot: list[Message]) -> None:
        if not self._cfg.agent.subconscious_summarize_before_compress:
            return
        if self._has_active_mode("summarize"):
            logger.debug("Subconscious pre-compress summarize skipped: already running")
            return
        if not short_term_snapshot:
            logger.debug(
                "Subconscious pre-compress summarize skipped: nothing to be compressed yet"
            )
            return
        mode = self._mode_loader.get("summarize")
        if mode is None:
            logger.warning(
                "Subconscious pre-compress summarize skipped: 'summarize' mode not loaded"
            )
            return
        goal = tr("subconscious.pre_compress_goal")
        await self._spawn(mode, short_term_snapshot, goal_override=goal)
        # Don't update _last_* so the periodic timer still fires normally.

    # ------------------------------------------------------------------
    # Mode reload (enables write_file → immediate effect without restart)
    # ------------------------------------------------------------------

    def _maybe_reload_modes(self, now: float) -> None:
        if now - self._last_mode_reload < _MODE_RELOAD_INTERVAL:
            return
        self._last_mode_reload = now
        old_enabled = {m.name for m in self._mode_loader.list_all()}
        old_hashes = dict(self._mode_content_hash)
        self._mode_loader.load_all()
        new_enabled = {m.name for m in self._mode_loader.list_all()}
        now_wall = datetime.now().timestamp()

        # Detect content changes in surviving modes.
        for mode in self._mode_loader.list_all():
            new_hash = _mode_hash(mode)
            if old_hashes.get(mode.name) != new_hash:
                self._mode_content_hash[mode.name] = new_hash
                self._mode_last_changed_wall[mode.name] = now_wall
                logger.info(f"Subconscious mode '{mode.name}' changed (content hash updated)")

        for name in new_enabled - old_enabled:
            self._last_cycle.setdefault(name, 0)
            self._last_time.setdefault(name, now)
            self._last_tool_calls.setdefault(name, 0)
            self._active_by_mode.setdefault(name, None)
            loaded_mode = self._mode_loader.get(name)
            if loaded_mode and name not in self._mode_content_hash:
                self._mode_content_hash[name] = _mode_hash(loaded_mode)
                self._mode_last_changed_wall[name] = now_wall  # new mode counts as changed

        for name in old_enabled - new_enabled:
            if self._active_by_mode.get(name) is None:
                self._active_by_mode.pop(name, None)
                self._last_cycle.pop(name, None)
                self._last_time.pop(name, None)
                self._last_tool_calls.pop(name, None)

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def _due(self, mode: SubconsciousMode, cycle_count: int, now: float) -> bool:
        name = mode.name
        if mode.trigger == "periodic":
            n = mode.every_n_cycles
            s = mode.every_seconds
            t = mode.every_n_tool_calls
            last_c = self._last_cycle.get(name, 0)
            last_t = self._last_time.get(name, now)
            last_tc = self._last_tool_calls.get(name, 0)
            return (
                (n > 0 and cycle_count - last_c >= n)
                or (s > 0 and now - last_t >= s)
                or (t > 0 and self._total_tool_calls - last_tc >= t)
            )
        if mode.trigger == "manual":
            return False
        if mode.trigger == "garden":
            return self._has_debt_due_palace(now) or self._has_stale_palace(now)
        # cold_floor
        cf = mode.cold_floor_seconds
        if cf <= 0:
            return False
        last_t = self._last_time.get(name, now - cf - 1)
        return now - last_t >= cf

    async def _dispatch(
        self,
        mode: SubconsciousMode,
        cycle_count: int,
        now: float,
        snapshot: list[Message],
    ) -> None:
        if mode.context_builder == "garden":
            await self._spawn_garden(now)
            return

        ctx: list[Message] = [] if mode.fresh_start else list(snapshot)
        if mode.inject_skill_anomalies:
            anomaly = self._build_skill_anomaly_message()
            if anomaly is not None:
                ctx.append(anomaly)
        if mode.inject_telemetry:
            ctx.append(self._build_telemetry_message())

        await self._spawn(mode, ctx)
        self._last_cycle[mode.name] = cycle_count
        self._last_time[mode.name] = now
        self._last_tool_calls[mode.name] = self._total_tool_calls

    # ------------------------------------------------------------------
    # Spawning
    # ------------------------------------------------------------------

    async def _spawn(
        self,
        mode: SubconsciousMode,
        forked_context: list[Message],
        goal_override: str | None = None,
    ) -> None:
        goal = goal_override or mode.goal or self._build_goal(mode.name)
        max_cycles = mode.max_cycles or self._cfg.agent.subconscious_max_cycles
        result = self._bubble_store.create(
            goal=goal,
            forked_context=forked_context,
            max_cycles=max_cycles,
        )
        if isinstance(result, str):
            logger.debug(f"Subconscious {mode.name} skipped: {result}")
            return
        bubble = result

        bubble_brain = self._create_brain()
        bubble.brain = bubble_brain
        system_prompt = self._prompt_builder.build()

        mini_loop = SubconsciousMiniLoop(
            mode=mode.name,
            identity_body=mode.body,
            intercepts=self._resolve_intercepts(mode),
            bubble=bubble,
            brain=bubble_brain,
            tool_registry=self._tools,
            system_prompt=system_prompt,
            bubble_store=self._bubble_store,
            inbox_watcher=self._inbox,
            logs_dir=str(Path(self._logs_dir) / "subconscious"),
            usage_stats=self._usage_stats,
            usage_logs_root=self._logs_dir,
            task_store=self._task_store if mode.grants_task_store else None,
        )
        task = asyncio.create_task(
            bind_locale(mini_loop.run),
            name=f"subconscious-{mode.name}-{bubble.id}",
        )
        bubble.task = task
        self._active_by_mode[mode.name] = bubble.id
        task.add_done_callback(lambda t: self._on_done(mode.name, bubble.id, t))
        if self._ilog:
            self._ilog.log_subconscious_spawned(mode=mode.name, bubble_id=bubble.id, goal=goal)
        logger.info(f"Subconscious {mode.name} spawned: {bubble.id}")

    def _resolve_intercepts(self, mode: SubconsciousMode) -> dict[str, str]:
        """Return the intercepts dict for a mode.

        Empty extra_intercepts → use the built-in full set.
        Non-empty → restrict to those names, with reasons from the built-in map
        (or a generic fallback reason for unknown names).
        """
        if not mode.extra_intercepts:
            return _subconscious_extra_intercepts()
        builtins = _subconscious_extra_intercepts()
        return {
            name: builtins.get(name, tr("subconscious.intercept_default", name=name))
            for name in mode.extra_intercepts
        }

    # ------------------------------------------------------------------
    # Garden (special spawn path)
    # ------------------------------------------------------------------

    def _tally_palace_usage(self) -> None:
        if self._palace_loader is None:
            return
        known = list(self._bubble_store._history) + self._bubble_store.list_active()
        known_ids = {b.id for b in known}
        valid_names = set(self._palace_loader.list_names())
        for b in known:
            if b.id in self._counted_bubble_ids:
                continue
            if b.status == "done" and b.palaces:
                self._counted_bubble_ids.add(b.id)
                for name in b.palaces:
                    if name in valid_names:
                        self._palace_use_counts[name] = self._palace_use_counts.get(name, 0) + 1
        self._counted_bubble_ids &= known_ids

    def _has_debt_due_palace(self, now: float) -> bool:
        garden = self._mode_loader.get("garden")
        if garden is None:
            return False
        threshold = garden.use_threshold
        if threshold <= 0:
            return False
        cooldown = garden.min_interval_seconds
        for name, count in self._palace_use_counts.items():
            last = self._palace_last_garden_time.get(name)
            if count >= threshold and (last is None or now - last >= cooldown):
                return True
        return False

    def _has_stale_palace(self, now: float) -> bool:
        if self._palace_loader is None:
            return False
        garden = self._mode_loader.get("garden")
        if garden is None:
            return False
        s = garden.every_seconds
        if s <= 0:
            return False
        for p in self._palace_loader.list_all():
            last = self._palace_last_garden_time.get(p.name)
            if last is None or now - last >= s:
                return True
        return False

    def _select_garden_palace(self, palaces: list[Palace], now: float) -> Palace | None:
        garden = self._mode_loader.get("garden")
        if garden is None:
            return None
        threshold = garden.use_threshold
        cooldown = garden.min_interval_seconds
        s = garden.every_seconds

        def in_cooldown(p: Palace) -> bool:
            last = self._palace_last_garden_time.get(p.name)
            return last is not None and now - last < cooldown

        def is_stale(p: Palace) -> bool:
            if s <= 0:
                return False
            last = self._palace_last_garden_time.get(p.name)
            return last is None or now - last >= s

        if threshold > 0:
            due = [
                p
                for p in palaces
                if self._palace_use_counts.get(p.name, 0) >= threshold and not in_cooldown(p)
            ]
            if due:
                due.sort(key=lambda p: self._palace_use_counts.get(p.name, 0), reverse=True)
                return due[0]

        n = len(palaces)
        for i in range(n):
            p = palaces[(self._garden_index + i) % n]
            if is_stale(p) and not in_cooldown(p):
                self._garden_index = (self._garden_index + i + 1) % n
                return p
        return None

    async def _spawn_garden(self, now: float) -> bool:
        from coworker.core.types import Message

        garden = self._mode_loader.get("garden")
        if garden is None:
            logger.debug("Subconscious garden skipped: 'garden' mode not loaded")
            return False
        if self._palace_loader is None or self._long_term is None or self._long_term._mem is None:
            return False
        palaces = self._palace_loader.list_all()
        if not palaces:
            logger.debug("Subconscious garden skipped: no palaces")
            return False
        palace = self._select_garden_palace(palaces, now)
        if palace is None:
            logger.debug("Subconscious garden skipped: all candidate palaces in cooldown")
            return False

        self._palace_use_counts[palace.name] = 0
        self._palace_last_garden_time[palace.name] = now

        ctx: list[Message] = [
            Message(
                role="system",
                content=tr("subconscious.palace", name=palace.name, body=palace.body),
            )
        ]
        mem_count = 0
        if palace.memory_tags:
            try:
                mems = await self._long_term.query_by_tags(
                    palace.when_to_attach or palace.name, palace.memory_tags, limit=30
                )
            except Exception:
                mems = []
            mem_count = len(mems)
            if mems:
                lines = [
                    tr(
                        "subconscious.palace_memory_title",
                        name=palace.name,
                        tags=", ".join(palace.memory_tags),
                        count=mem_count,
                    )
                ]
                for i, m in enumerate(mems, 1):
                    lines.append(
                        tr(
                            "subconscious.memory_item",
                            index=i,
                            id=m["id"],
                            category=m["category"],
                            content=m["content"],
                            relevance=f"{m['relevance']:.2f}",
                        )
                    )
                ctx.append(Message(role="system", content="\n".join(lines)))

        if mem_count == 0:
            logger.debug(
                f"Subconscious garden '{palace.name}': no tagged memories yet, spawning for discovery"
            )

        goal = tr(
            "subconscious.garden_goal",
            name=palace.name,
            tags=", ".join(palace.memory_tags) or tr("subconscious.none"),
        )
        await self._spawn(garden, ctx, goal_override=goal)
        return True

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def _build_telemetry_message(self) -> Message:
        from coworker.core.types import Message as _Msg

        meta_last_run = self._mode_last_run_wall.get("meta")

        lines = [tr("subconscious.telemetry_title")]
        changed_since_meta: list[tuple[str, float]] = []

        for mode in self._mode_loader.list_all():
            run_count = self._mode_run_count.get(mode.name, 0)
            last_run_wall = self._mode_last_run_wall.get(mode.name)
            last_run_str = (
                datetime.fromtimestamp(last_run_wall).strftime("%Y-%m-%d %H:%M")
                if last_run_wall is not None
                else tr("subconscious.never_run")
            )
            recent = self._mode_recent_results.get(mode.name, [])
            recent_str = "; ".join(recent[-3:]) if recent else tr("subconscious.no_recent")

            # Change status relative to last meta review.
            changed_wall = self._mode_last_changed_wall.get(mode.name)
            if changed_wall is not None and mode.name != "meta":
                changed_str = datetime.fromtimestamp(changed_wall).strftime("%Y-%m-%d %H:%M")
                if meta_last_run is None or changed_wall > meta_last_run:
                    change_tag = tr("subconscious.changed_unreviewed", time=changed_str)
                    changed_since_meta.append((mode.name, changed_wall))
                else:
                    change_tag = tr("subconscious.changed_reviewed", time=changed_str)
            else:
                change_tag = ""

            if mode.trigger == "periodic":
                cfg_str = (
                    f"every_n_cycles={mode.every_n_cycles} "
                    f"every_seconds={mode.every_seconds} "
                    f"every_n_tool_calls={mode.every_n_tool_calls}"
                )
            elif mode.trigger == "manual":
                cfg_str = "manual_only=true"
            elif mode.trigger == "garden":
                cfg_str = (
                    f"use_threshold={mode.use_threshold} "
                    f"every_seconds={mode.every_seconds} "
                    f"min_interval={mode.min_interval_seconds}"
                )
            else:
                cfg_str = f"cold_floor_seconds={mode.cold_floor_seconds}"

            max_c = mode.max_cycles or "default"
            lifecycle_tags = []
            if mode.protected:
                lifecycle_tags.append(tr("subconscious.protected"))
            if mode.retire_after:
                lifecycle_tags.append(tr("subconscious.retire", condition=mode.retire_after))
            lifecycle_str = f" [{', '.join(lifecycle_tags)}]" if lifecycle_tags else ""
            purpose_str = tr("subconscious.purpose", purpose=mode.purpose) if mode.purpose else ""
            lines.append(
                tr(
                    "subconscious.telemetry_line",
                    name=mode.name,
                    lifecycle=lifecycle_str,
                    purpose=purpose_str,
                    trigger=mode.trigger,
                    config=cfg_str,
                    max_cycles=max_c,
                    count=run_count,
                    last_run=last_run_str,
                    recent=recent_str,
                    change=change_tag,
                )
            )

        if len(lines) == 1:
            lines.append(tr("subconscious.no_telemetry"))

        # Change summary — the key signal for variable-depth review.
        lines.append("")
        if changed_since_meta:
            lines.append(tr("subconscious.change_summary", count=len(changed_since_meta)))
            for name, wall in sorted(changed_since_meta, key=lambda x: -x[1]):
                when = datetime.fromtimestamp(wall).strftime("%Y-%m-%d %H:%M")
                lines.append(tr("subconscious.change_item", name=name, time=when))
            lines.append(tr("subconscious.change_advice"))
        else:
            meta_last_str = (
                datetime.fromtimestamp(meta_last_run).strftime("%Y-%m-%d %H:%M")
                if meta_last_run
                else tr("subconscious.never")
            )
            lines.append(tr("subconscious.no_changes", time=meta_last_str))

        return _Msg(role="system", content="\n".join(lines))

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _has_active_mode(self, mode: str) -> bool:
        bid = self._active_by_mode.get(mode)
        if not bid:
            return False
        bubble = self._bubble_store._active.get(bid)
        if bubble is None or bubble.is_terminal():
            self._active_by_mode[mode] = None
            return False
        return True

    def _on_done(self, mode: str, bubble_id: str, _task: asyncio.Task) -> None:
        bubble = self._bubble_store.get(bubble_id)
        if bubble and self._ilog:
            self._ilog.log_subconscious_done(
                mode=mode,
                bubble_id=bubble_id,
                result=bubble.result,
                cycles=bubble.cycles_used,
                elapsed_s=bubble.elapsed_seconds(),
            )
        if self._active_by_mode.get(mode) == bubble_id:
            self._active_by_mode[mode] = None

        # Telemetry collection.
        now_wall = datetime.now().timestamp()
        self._mode_run_count[mode] = self._mode_run_count.get(mode, 0) + 1
        self._mode_last_run_wall[mode] = now_wall
        results = self._mode_recent_results.setdefault(mode, [])
        if bubble:
            results.append((bubble.result or "")[:200])
        del results[:-5]

        logger.info(
            f"Subconscious {mode} done: {bubble_id}, "
            f"status={bubble.status if bubble else 'unknown'}"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_goal(self, mode_name: str) -> str:
        if mode_name == "audit":
            return tr("subconscious.goal_audit")
        if mode_name == "explore":
            return tr("subconscious.goal_explore")
        if mode_name == "introspect":
            return tr("subconscious.goal_introspect")
        if mode_name == "garden":
            return tr("subconscious.goal_garden")
        if mode_name == "meta":
            return tr("subconscious.goal_meta")
        return tr("subconscious.goal_summarize")

    def _build_skill_anomaly_message(self):
        from coworker.core.types import Message

        anomalies = self._scan_skill_anomalies()
        if not anomalies:
            return None
        lines = [tr("subconscious.skill_anomaly")]
        lines.extend(f"- {a}" for a in anomalies)
        return Message(role="system", content="\n".join(lines))

    def _scan_skill_anomalies(self) -> list[str]:
        try:
            d = Path(self._cfg.agent.skills_dir)
        except Exception:
            return []
        if not d.exists():
            return []
        out: list[str] = []
        for entry in sorted(d.iterdir()):
            if entry.is_file() and entry.suffix == ".md":
                out.append(entry.name)
            elif entry.is_dir() and not (entry / "SKILL.md").exists():
                out.append(tr("subconscious.missing_skill", name=entry.name))
        return out

    def _create_brain(self) -> Brain:
        from coworker.brain.brain import Brain as _Brain

        new_brain = _Brain(
            default_provider=self._brain.current_provider_name,
            default_model=self._brain.current_model,
            message_time_prefix=self._brain.message_time_prefix,
            max_tokens=self._brain.max_tokens,
            summary_provider=self._brain.summary_provider_name,
            summary_model=self._brain.summary_model,
            summary_thinking=self._brain.summary_thinking,
            vision_provider=self._brain.vision_provider_name,
            vision_model=self._brain.vision_model,
            vision_thinking=self._brain.vision_thinking,
        )
        for provider in self._brain._providers.values():
            new_brain.register_provider(provider)
        return new_brain

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def save_state(self) -> None:
        if self._state_path is None:
            return
        now_mono = time.monotonic()
        now_wall = datetime.now().timestamp()

        modes_data: dict[str, dict] = {}
        all_mode_names = set(self._last_time) | set(self._mode_run_count)
        for mode_name in all_mode_names:
            md: dict = {}
            md["last_tool_calls"] = self._last_tool_calls.get(mode_name, 0)
            last_t = self._last_time.get(mode_name)
            if last_t is not None:
                md["last_wall"] = now_wall - (now_mono - last_t)
            run_count = self._mode_run_count.get(mode_name, 0)
            if run_count:
                md["run_count"] = run_count
            last_run_wall = self._mode_last_run_wall.get(mode_name)
            if last_run_wall is not None:
                md["last_run_wall"] = last_run_wall
            recent = self._mode_recent_results.get(mode_name, [])
            if recent:
                md["recent_results"] = recent
            content_hash = self._mode_content_hash.get(mode_name)
            if content_hash:
                md["content_hash"] = content_hash
            last_changed_wall = self._mode_last_changed_wall.get(mode_name)
            if last_changed_wall is not None:
                md["last_changed_wall"] = last_changed_wall
            modes_data[mode_name] = md

        data = {
            "total_tool_calls": self._total_tool_calls,
            "garden_index": self._garden_index,
            "palace_use_counts": self._palace_use_counts,
            "palace_last_garden_wall": {
                name: now_wall - (now_mono - t) for name, t in self._palace_last_garden_time.items()
            },
            "modes": modes_data,
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(data), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to save subconscious state: {e}")

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Failed to load subconscious state: {e}")
            return

        self._total_tool_calls = data.get("total_tool_calls", 0)
        self._garden_index = data.get("garden_index", 0)

        loaded_counts = data.get("palace_use_counts")
        if isinstance(loaded_counts, dict):
            self._palace_use_counts = {str(k): int(v) for k, v in loaded_counts.items()}

        now_mono = time.monotonic()
        now_wall = datetime.now().timestamp()

        loaded_garden = data.get("palace_last_garden_wall")
        if isinstance(loaded_garden, dict):
            self._palace_last_garden_time = {
                str(name): now_mono - max(0.0, now_wall - float(w))
                for name, w in loaded_garden.items()
            }

        # --- Old format migration (pre-modes-dict) ---
        if "modes" not in data:
            for mode_name in ("audit", "summarize", "explore", "introspect"):
                old_tc = data.get(f"last_{mode_name}_tool_calls")
                if old_tc is not None:
                    self._last_tool_calls[mode_name] = int(old_tc)
                old_wall = data.get(f"last_{mode_name}_wall")
                if old_wall is not None:
                    elapsed = max(0.0, now_wall - float(old_wall))
                    self._last_time[mode_name] = now_mono - elapsed
            logger.debug("Subconscious scheduling state restored (old format)")
            return

        # --- New format ---
        modes_data = data.get("modes", {})
        for mode_name, md in modes_data.items():
            if not isinstance(md, dict):
                continue
            tc = md.get("last_tool_calls")
            if tc is not None:
                self._last_tool_calls[mode_name] = int(tc)
            last_wall = md.get("last_wall")
            if last_wall is not None:
                elapsed = max(0.0, now_wall - float(last_wall))
                self._last_time[mode_name] = now_mono - elapsed
            run_count = md.get("run_count", 0)
            if run_count:
                self._mode_run_count[mode_name] = int(run_count)
            last_run_wall = md.get("last_run_wall")
            if last_run_wall is not None:
                self._mode_last_run_wall[mode_name] = float(last_run_wall)
            recent = md.get("recent_results", [])
            if recent:
                self._mode_recent_results[mode_name] = [str(r) for r in recent[-5:]]
            content_hash = md.get("content_hash")
            if content_hash:
                self._mode_content_hash[mode_name] = str(content_hash)
            last_changed_wall = md.get("last_changed_wall")
            if last_changed_wall is not None:
                self._mode_last_changed_wall[mode_name] = float(last_changed_wall)

        logger.debug("Subconscious scheduling state restored")
