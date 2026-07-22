"""每个分支一个独立子进程的控制服务。

启动：`python -m explore_lab.branch_runner --workdir <dir> --control-port <port>`

装配对象图后停在 `paused` 状态，靠 `step`/`step_n`/`resume` 主动驱动
`AgentLoop._cycle()`；不复用生产的 `AgentLoop.run()`（那套"连续错误自动截断
恢复"逻辑是给生产环境兜底的，调试时应该让错误原样冒出来）。

控制端口只 bind 127.0.0.1：这套 control API 本身没有鉴权，一旦暴露到网络上
等于任何人都能操控分支、读到内存里的 API key。
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import uvicorn
from coworker.core.types import IncomingEvent, Message
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from explore_lab.assembly import (
    Runtime,
    assemble_runtime,
    create_subconscious_scheduler,
    set_tool_intercepts,
)
from explore_lab.undo import Snapshot, UndoStack, capture_agent_state, restore_agent_state

_DEFAULT_RESUME_MAX_CYCLES = 50
_DEFAULT_RESUME_MAX_SECONDS = 1800.0
_SUBCONSCIOUS_AWAIT_TIMEOUT = 120.0


class BranchStatus(StrEnum):
    STARTING = "starting"
    PAUSED = "paused"
    STEPPING = "stepping"
    RUNNING = "running"
    CRASHED = "crashed"
    STOPPED = "stopped"


class ConflictError(Exception):
    def __init__(self, current_status: BranchStatus) -> None:
        self.current_status = current_status
        super().__init__(f"invalid transition from {current_status.value}")


def _message_to_dict(m: Message) -> dict[str, Any]:
    d = m.to_dict()
    d["timestamp"] = m.timestamp.isoformat()
    return d


class BranchController:
    def __init__(self) -> None:
        self.runtime: Runtime | None = None
        self.status = BranchStatus.STARTING
        # 状态机的互斥性靠"检查 status 与切换 status 之间不出现 await"来保证：asyncio 单线程
        # 协作式调度下这段代码天然原子，两个并发控制请求里必有一个在切换前就读到非 PAUSED
        # 状态从而立即 409——不需要真的引入 asyncio.Lock（那样反而会把"立即拒绝"变成"排队等待"，
        # 与"不满足前置状态直接 409、不做隐式排队重试"的设计相悖）。
        self.undo = UndoStack()
        self.system_prompt_override: str | None = None
        self._original_prompt_build: Callable[[], str] | None = None
        self.last_error: dict[str, Any] | None = None
        self._resume_task: asyncio.Task | None = None
        self._resume_stop = asyncio.Event()
        self._auto_paused_reason: str | None = None

    # ------------------------------------------------------------------
    # 启动 / 装配
    # ------------------------------------------------------------------

    async def start(self, workdir: Path) -> None:
        try:
            self.runtime = await assemble_runtime(workdir)
            self._wrap_prompt_builder()
            self.status = BranchStatus.PAUSED
        except Exception as e:
            self.last_error = {"type": type(e).__name__, "message": str(e)}
            self.status = BranchStatus.CRASHED

    def _wrap_prompt_builder(self) -> None:
        """在 branch_runner 自己的包装层加一层覆盖判断，不改 SystemPromptBuilder 本身。"""
        assert self.runtime is not None
        prompt_builder = self.runtime.prompt_builder
        original_build = prompt_builder.build
        self._original_prompt_build = original_build

        def _build_with_override() -> str:
            if self.system_prompt_override is not None:
                return self.system_prompt_override
            return original_build()

        prompt_builder.build = _build_with_override  # type: ignore[method-assign]

    def _require_runtime(self) -> Runtime:
        if self.runtime is None:
            raise HTTPException(
                status_code=503, detail=f"branch not ready (status={self.status.value})",
            )
        return self.runtime

    # ------------------------------------------------------------------
    # 快照 / 撤销
    # ------------------------------------------------------------------

    def _read_thinking(self) -> str | None:
        path = self.runtime.thinking_path if self.runtime else None
        if path is None or not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def _write_thinking(self, content: str | None) -> None:
        if self.runtime is None:
            return
        path = self.runtime.thinking_path
        if content is None:
            if path.exists():
                path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _active_bubble_ids(self) -> frozenset[str]:
        if self.runtime is None or self.runtime.bubble_store is None:
            return frozenset()
        return frozenset(b.id for b in self.runtime.bubble_store.list_active())

    def _capture_snapshot(self) -> Snapshot:
        rt = self._require_runtime()
        return Snapshot(
            short_term_data=rt.short_term.serialize(),
            agent_state=capture_agent_state(rt.agent_state),
            thinking_md=self._read_thinking(),
            active_bubble_ids=self._active_bubble_ids(),
        )

    def _restore_snapshot(self, snap: Snapshot) -> None:
        rt = self._require_runtime()
        # ShortTermMemory.deserialize 会 cls(**kwargs) 新建实例；这里改成原地灌数据到
        # 现有实例，避免 AgentLoop/工具持有的旧 ShortTermMemory 引用失效。kwargs 必须和
        # assemble_runtime 时完全一致，否则压缩阈值等运行时参数会被悄悄重置成默认值。
        restored = type(rt.short_term).deserialize(snap.short_term_data, **rt.stm_kwargs)
        rt.short_term.primary = restored.primary
        rt.short_term.pinned_items = restored.pinned_items
        rt.short_term.threads = restored.threads
        rt.short_term.active_provider = restored.active_provider
        rt.short_term.active_model = restored.active_model
        rt.short_term.tree = restored.tree

        restore_agent_state(rt.agent_state, snap.agent_state)
        self._write_thinking(snap.thinking_md)

        if rt.bubble_store is not None:
            current_ids = self._active_bubble_ids()
            newly_spawned = current_ids - snap.active_bubble_ids
            for bubble_id in newly_spawned:
                bubble = rt.bubble_store.get(bubble_id)
                if bubble is not None and bubble.task is not None and not bubble.task.done():
                    bubble.task.cancel()

    # ------------------------------------------------------------------
    # 潜意识后台任务同步
    # ------------------------------------------------------------------

    def _active_subconscious_bubble_ids(self) -> set[str]:
        rt = self._require_runtime()
        if rt.subconscious is None:
            return set()
        return {v for v in rt.subconscious._active_by_mode.values() if v}

    async def _await_subconscious(self, timeout: float = _SUBCONSCIOUS_AWAIT_TIMEOUT) -> None:
        rt = self._require_runtime()
        if rt.bubble_store is None:
            return
        ids = self._active_subconscious_bubble_ids()
        tasks = []
        for bid in ids:
            bubble = rt.bubble_store.get(bid)
            if bubble is not None and bubble.task is not None and not bubble.task.done():
                tasks.append(bubble.task)
        if not tasks:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
        except TimeoutError:
            pass

    # ------------------------------------------------------------------
    # 单步推进
    # ------------------------------------------------------------------

    async def _run_one_cycle(self, *, await_subconscious: bool = True) -> dict[str, Any]:
        rt = self._require_runtime()
        snapshot = self._capture_snapshot()
        before_len = len(rt.short_term.primary)
        try:
            await rt.agent_loop._cycle()
        except Exception as e:
            self._restore_snapshot(snapshot)
            return {
                "ok": False,
                "error": {"type": type(e).__name__, "message": str(e)},
                "new_messages": [],
            }
        self.undo.push(snapshot)
        if await_subconscious:
            await self._await_subconscious()
        new_messages = [_message_to_dict(m) for m in rt.short_term.primary[before_len:]]
        return {"ok": True, "error": None, "new_messages": new_messages}

    async def step(self, *, await_subconscious: bool = True) -> dict[str, Any]:
        if self.status != BranchStatus.PAUSED:
            raise ConflictError(self.status)
        self.status = BranchStatus.STEPPING
        try:
            result = await self._run_one_cycle(await_subconscious=await_subconscious)
        finally:
            self.status = BranchStatus.PAUSED
        return result

    async def step_n(
        self,
        n: int,
        stop_condition: Literal["n_cycles", "until_reply"] | None = None,
        *,
        await_subconscious: bool = True,
    ) -> dict[str, Any]:
        if self.status != BranchStatus.PAUSED:
            raise ConflictError(self.status)
        self.status = BranchStatus.STEPPING
        completed = 0
        stopped_early: str | None = None
        all_new_messages: list[dict[str, Any]] = []
        try:
            rt = self._require_runtime()
            for _ in range(n):
                result = await self._run_one_cycle(await_subconscious=await_subconscious)
                completed += 1
                all_new_messages.extend(result["new_messages"])
                if not result["ok"]:
                    stopped_early = "error"
                    return {
                        "ok": False,
                        "completed": completed,
                        "stopped_early": stopped_early,
                        "error": result["error"],
                        "new_messages": all_new_messages,
                    }
                if stop_condition == "until_reply":
                    last_assistant = next(
                        (m for m in reversed(rt.short_term.primary) if m.role == "assistant"),
                        None,
                    )
                    if last_assistant is not None and not last_assistant.tool_calls:
                        stopped_early = "until_reply"
                        break
        finally:
            self.status = BranchStatus.PAUSED
        return {
            "ok": True,
            "completed": completed,
            "stopped_early": stopped_early,
            "error": None,
            "new_messages": all_new_messages,
        }

    async def back_step(self) -> dict[str, Any]:
        if self.status != BranchStatus.PAUSED:
            raise ConflictError(self.status)
        snap = self.undo.pop()
        if snap is None:
            raise HTTPException(status_code=409, detail="no earlier state to back-step to")
        self._restore_snapshot(snap)
        return {"ok": True, "undo_depth": len(self.undo)}

    async def back_step_n(self, n: int) -> dict[str, Any]:
        if self.status != BranchStatus.PAUSED:
            raise ConflictError(self.status)
        stepped = 0
        last_snap: Snapshot | None = None
        for _ in range(n):
            snap = self.undo.pop()
            if snap is None:
                break
            last_snap = snap
            stepped += 1
        if last_snap is not None:
            self._restore_snapshot(last_snap)
        return {"ok": True, "stepped": stepped, "undo_depth": len(self.undo)}

    # ------------------------------------------------------------------
    # pause / resume
    # ------------------------------------------------------------------

    async def pause(self) -> dict[str, Any]:
        if self.status != BranchStatus.RUNNING:
            raise ConflictError(self.status)
        self._resume_stop.set()
        if self._resume_task is not None:
            await self._resume_task
        return {"ok": True, "status": self.status.value}

    async def resume(self, max_cycles: int | None, max_seconds: float | None) -> dict[str, Any]:
        if self.status != BranchStatus.PAUSED:
            raise ConflictError(self.status)
        max_cycles = max_cycles or _DEFAULT_RESUME_MAX_CYCLES
        max_seconds = max_seconds or _DEFAULT_RESUME_MAX_SECONDS
        self._resume_stop = asyncio.Event()
        self.status = BranchStatus.RUNNING

        async def _run() -> None:
            started = time.monotonic()
            cycles = 0
            reason = "stopped"
            try:
                while not self._resume_stop.is_set():
                    if cycles >= max_cycles:
                        reason = "max_cycles"
                        break
                    if time.monotonic() - started >= max_seconds:
                        reason = "max_seconds"
                        break
                    result = await self._run_one_cycle()
                    cycles += 1
                    if not result["ok"]:
                        reason = "error"
                        break
                    rt = self._require_runtime()
                    last = rt.short_term.primary[-1] if rt.short_term.primary else None
                    inbox_idle = not rt.inbox_watcher.message_event.is_set()
                    settled = last is not None and last.role == "assistant" and not last.tool_calls
                    if inbox_idle and settled and not rt.agent_state.tick:
                        # inbox 空闲且模型已经"说完话"（无 tool_calls）：这轮任务已经跑到尽头，
                        # 没有继续 step 的意义，自动转回 paused 而不是空转烧 token。
                        reason = "idle"
                        break
                else:
                    reason = "paused"
            finally:
                self._auto_paused_reason = reason
                self.status = BranchStatus.PAUSED

        self._resume_task = asyncio.create_task(_run())
        return {"ok": True, "status": self.status.value}

    # ------------------------------------------------------------------
    # 输入 / system prompt / config
    # ------------------------------------------------------------------

    async def push_input(
        self, content: str, participant_id: str, conversation_id: str | None,
    ) -> str:
        rt = self._require_runtime()
        event = IncomingEvent(
            participant_id=participant_id,
            content=content,
            conversation_id=conversation_id,
            source="rest",
        )
        return await rt.inbox_watcher.push(event)

    def set_system_prompt_override(self, text: str | None) -> None:
        self.system_prompt_override = text

    def current_system_prompt(self) -> dict[str, Any]:
        rt = self._require_runtime()
        base_build = self._original_prompt_build or rt.prompt_builder.build
        base_text = base_build()
        effective_text = (
            self.system_prompt_override
            if self.system_prompt_override is not None
            else base_text
        )
        return {
            "base_text": base_text,
            "effective_text": effective_text,
            "override_active": self.system_prompt_override is not None,
            "override_text": self.system_prompt_override,
        }

    def patch_tool_intercepts(self, intercepts: dict[str, str]) -> None:
        rt = self._require_runtime()
        set_tool_intercepts(rt, intercepts)

    def set_virtual_connections(self, participant_ids: list[str]) -> list[str]:
        rt = self._require_runtime()
        rt.communicate.set_virtual_connections(participant_ids)
        return rt.communicate.virtual_connections()

    def _set_subconscious_runtime_refs(self, scheduler) -> None:
        rt = self._require_runtime()
        rt.agent_loop._subconscious = scheduler
        rt.clear_short_term_memory_tool._subconscious = scheduler

    def _persist_config(self) -> None:
        rt = self._require_runtime()
        config_path = rt.workdir / "config.json"
        config_path.write_text(rt.config.model_dump_json(indent=2), encoding="utf-8")

    def set_subconscious_enabled(self, enabled: bool) -> dict[str, Any]:
        rt = self._require_runtime()
        rt.config.agent.subconscious_thinking = enabled
        if enabled:
            if rt.subconscious is None:
                rt.subconscious = create_subconscious_scheduler(rt)
            self._set_subconscious_runtime_refs(rt.subconscious)
        else:
            # Keep rt.subconscious alive so already-spawned subconscious bubbles remain
            # visible as subconscious until they finish, but disconnect all new triggers.
            self._set_subconscious_runtime_refs(None)
        self._persist_config()
        return {
            "enabled": rt.config.agent.subconscious_thinking,
            "pending": sorted(self._active_subconscious_bubble_ids()),
        }

    async def patch_hot_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        rt = self._require_runtime()
        applied: dict[str, Any] = {}

        llm = payload.get("llm") or {}
        if "default_provider" in llm or "default_model" in llm:
            provider = llm.get("default_provider", rt.brain.current_provider_name)
            model = llm.get("default_model", "")
            await rt.brain.switch_model(provider, model)
            applied["llm.default_provider"] = provider
            applied["llm.default_model"] = model or rt.brain.current_model
        hot_llm_fields = (
            "summary_provider", "summary_model", "summary_thinking",
            "fallbacks", "vision_provider", "vision_model",
        )
        for key in hot_llm_fields:
            if key in llm:
                setattr(rt.config.llm, key, llm[key])
                applied[f"llm.{key}"] = llm[key]

        memory = payload.get("memory") or {}
        for key in ("auto_recall_enabled", "auto_recall_relevance_threshold", "auto_recall_limit"):
            if key in memory:
                setattr(rt.config.memory, key, memory[key])
                applied[f"memory.{key}"] = memory[key]

        agent = payload.get("agent") or {}
        for key in ("inbox_batch_max", "tick", "message_time_prefix"):
            if key in agent:
                setattr(rt.config.agent, key, agent[key])
                applied[f"agent.{key}"] = agent[key]
        if "subconscious_thinking" in agent:
            enabled = agent["subconscious_thinking"]
            if not isinstance(enabled, bool):
                raise HTTPException(
                    status_code=422, detail="agent.subconscious_thinking must be boolean"
                )
            result = self.set_subconscious_enabled(enabled)
            applied["agent.subconscious_thinking"] = result["enabled"]

        if "tool_intercepts" in payload:
            self.patch_tool_intercepts(payload["tool_intercepts"])
            applied["tool_intercepts"] = payload["tool_intercepts"]
        if "virtual_connections" in payload:
            virtual_connections = payload["virtual_connections"]
            if not isinstance(virtual_connections, list) or not all(
                isinstance(item, str) for item in virtual_connections
            ):
                raise HTTPException(
                    status_code=422,
                    detail="virtual_connections must be a list of strings",
                )
            applied["virtual_connections"] = self.set_virtual_connections(
                virtual_connections
            )

        return applied

    def flush_snapshot(self) -> dict[str, Any]:
        """把当前内存里的 short_term 落盘到 workdir。fork 靠 `shutil.copytree` 复制
        workdir，必须先调用这个把最新对话状态写回磁盘，否则 fork 出来的是导入时的
        陈旧快照——分支手动 step 期间不会像生产 `AgentLoop.run()` 那样每轮自动落盘。"""
        rt = self._require_runtime()
        rt.short_term.active_provider = rt.brain.current_provider_name
        rt.short_term.active_model = rt.brain.current_model
        rt.short_term.save_to_file(rt.snapshot_path)
        return {"ok": True, "snapshot_path": str(rt.snapshot_path)}

    # ------------------------------------------------------------------
    # 状态读取
    # ------------------------------------------------------------------

    def state_snapshot(self) -> dict[str, Any]:
        if self.runtime is None:
            return {
                "status": self.status.value,
                "last_error": self.last_error,
            }
        rt = self.runtime
        return {
            "status": self.status.value,
            "auto_paused_reason": self._auto_paused_reason,
            "cycle_count": rt.agent_state.cycle_count,
            "current_provider": rt.brain.current_provider_name,
            "current_model": rt.brain.current_model,
            "tick": rt.agent_state.tick,
            "is_sleeping": rt.agent_state.is_sleeping,
            "tool_call_counts": dict(rt.agent_state.tool_call_counts),
            "transcript": [_message_to_dict(m) for m in rt.short_term.primary],
            "undo_depth": len(self.undo),
            "system_prompt_override_active": self.system_prompt_override is not None,
            "system_prompt_override_text": self.system_prompt_override,
            "tool_intercepts": dict(rt.tool_intercepts),
            "virtual_connections": rt.communicate.virtual_connections(),
            "outbound_messages": rt.communicate.outbound_messages(),
            "subconscious_enabled": rt.config.agent.subconscious_thinking,
            "usage_stats": rt.usage_stats.snapshot(),
            "active_bubbles": self._bubble_summaries(),
            "subconscious_pending": sorted(self._active_subconscious_bubble_ids()),
            "last_error": self.last_error,
        }

    def _bubble_summaries(self) -> list[dict[str, Any]]:
        rt = self.runtime
        if rt is None or rt.bubble_store is None:
            return []
        subconscious_ids = self._active_subconscious_bubble_ids()
        out = []
        for b in rt.bubble_store.list_active():
            out.append({
                "id": b.id,
                "goal": b.goal,
                "status": b.status,
                "cycles_used": b.cycles_used,
                "max_cycles": b.max_cycles,
                "participant_id": b.participant_id,
                "created_at": b.created_at.isoformat(),
                "kind": "subconscious" if b.id in subconscious_ids else "goal",
            })
        return out


controller = BranchController()


class StepNRequest(BaseModel):
    n: int = Field(gt=0)
    stop_condition: Literal["n_cycles", "until_reply"] | None = None
    await_subconscious: bool = True


class StepRequest(BaseModel):
    await_subconscious: bool = True


class BackStepNRequest(BaseModel):
    n: int = Field(gt=0)


class ResumeRequest(BaseModel):
    max_cycles: int | None = None
    max_seconds: float | None = None


class InputRequest(BaseModel):
    content: str
    participant_id: str = "explore_lab"
    conversation_id: str | None = None


class SystemPromptOverrideRequest(BaseModel):
    text: str | None = None


class SubconsciousTriggerRequest(BaseModel):
    mode: str


class SubconsciousAwaitRequest(BaseModel):
    timeout_seconds: float = _SUBCONSCIOUS_AWAIT_TIMEOUT


class SubconsciousEnabledRequest(BaseModel):
    enabled: bool


def _conflict_response(err: ConflictError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"message": "invalid state transition", "current_status": err.current_status.value},
    )


def create_app(workdir: Path) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        asyncio.create_task(controller.start(workdir))
        yield

    app = FastAPI(title="coworker-explore-lab branch_runner", lifespan=lifespan)
    # 只 bind 127.0.0.1，本身没有鉴权；放开 CORS 只是为了让同机浏览器里的前端能直接
    # 打这个端口（这套工具假设前端/orchestrator/branch_runner 都在同一台受信机器上）。
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )

    @app.get("/state")
    async def get_state():
        return controller.state_snapshot()

    @app.post("/snapshot")
    async def post_snapshot():
        return controller.flush_snapshot()

    @app.post("/control/step")
    async def control_step(body: StepRequest = StepRequest()):
        try:
            return await controller.step(await_subconscious=body.await_subconscious)
        except ConflictError as e:
            raise _conflict_response(e) from e

    @app.post("/control/step_n")
    async def control_step_n(body: StepNRequest):
        try:
            return await controller.step_n(
                body.n, body.stop_condition, await_subconscious=body.await_subconscious
            )
        except ConflictError as e:
            raise _conflict_response(e) from e

    @app.post("/control/back_step")
    async def control_back_step():
        try:
            return await controller.back_step()
        except ConflictError as e:
            raise _conflict_response(e) from e

    @app.post("/control/back_step_n")
    async def control_back_step_n(body: BackStepNRequest):
        try:
            return await controller.back_step_n(body.n)
        except ConflictError as e:
            raise _conflict_response(e) from e

    @app.post("/control/pause")
    async def control_pause():
        try:
            return await controller.pause()
        except ConflictError as e:
            raise _conflict_response(e) from e

    @app.post("/control/resume")
    async def control_resume(body: ResumeRequest = ResumeRequest()):
        try:
            return await controller.resume(body.max_cycles, body.max_seconds)
        except ConflictError as e:
            raise _conflict_response(e) from e

    @app.post("/input")
    async def post_input(body: InputRequest):
        event_id = await controller.push_input(
            body.content, body.participant_id, body.conversation_id,
        )
        return {"event_id": event_id}

    @app.patch("/system_prompt_override")
    async def patch_system_prompt_override(body: SystemPromptOverrideRequest):
        controller.set_system_prompt_override(body.text)
        return {"active": body.text is not None}

    @app.get("/system_prompt")
    async def get_system_prompt():
        return controller.current_system_prompt()

    @app.patch("/config")
    async def patch_config(payload: dict[str, Any]):
        applied = await controller.patch_hot_config(payload)
        return {"applied": applied}

    @app.get("/bubbles")
    async def get_bubbles():
        return {"bubbles": controller._bubble_summaries()}

    @app.get("/subconscious/pending")
    async def get_subconscious_pending():
        return {"pending": sorted(controller._active_subconscious_bubble_ids())}

    @app.patch("/subconscious/enabled")
    async def patch_subconscious_enabled(body: SubconsciousEnabledRequest):
        return controller.set_subconscious_enabled(body.enabled)

    @app.post("/subconscious/trigger")
    async def post_subconscious_trigger(body: SubconsciousTriggerRequest):
        rt = controller._require_runtime()
        if not rt.config.agent.subconscious_thinking or rt.subconscious is None:
            raise HTTPException(
                status_code=503, detail="subconscious_thinking is disabled on this branch",
            )
        mode = rt.mode_loader.get(body.mode)
        if mode is None:
            raise HTTPException(status_code=404, detail=f"unknown subconscious mode: {body.mode}")
        before = controller._active_subconscious_bubble_ids()
        await rt.subconscious._dispatch(
            mode,
            cycle_count=rt.agent_state.cycle_count,
            now=time.monotonic(),
            snapshot=list(rt.short_term.primary),
        )
        after = controller._active_subconscious_bubble_ids()
        spawned = sorted(after - before)
        return {"spawned_bubble_ids": spawned}

    @app.post("/subconscious/await")
    async def post_subconscious_await(body: SubconsciousAwaitRequest = SubconsciousAwaitRequest()):
        await controller._await_subconscious(timeout=body.timeout_seconds)
        return {"pending": sorted(controller._active_subconscious_bubble_ids())}

    @app.get("/events")
    async def get_events(request: Request):
        rt = controller._require_runtime()
        queue = rt.event_collector.register()

        async def event_stream():
            try:
                yield ": connected\n\n"
                for ev in rt.event_collector.recent(40):
                    yield f"data: {_json_dumps(ev)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield f"data: {_json_dumps(ev)}\n\n"
            finally:
                rt.event_collector.unregister(queue)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


def _json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--control-port", type=int, required=True)
    args = parser.parse_args()

    workdir = Path(args.workdir).resolve()
    import os

    os.chdir(workdir)

    app = create_app(workdir)
    uvicorn.run(app, host="127.0.0.1", port=args.control_port, log_level="warning")


if __name__ == "__main__":
    main()
