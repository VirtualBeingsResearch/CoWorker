"""OpenAI-compatible chat completion endpoints backed by the agent channels.

These endpoints expose the agent to any client that can talk to a chat model.
One request opens one conversation turn on the model API channel; the agent's
``communicate`` replies stream into the response, the reply marked
``extra={"end_turn": true}`` closes it, and a ``tool_calls`` reply hands
control back to the caller's application in OpenAI function-calling format.

Inbound caller material is delivered to the agent as separate inbound events —
the caller's system prompt and tool schemas as one raw scenario event, and each
new message as its own event — so the model, not the transport, interprets them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from coworker.channels.modelapi import (
    ModelApiChannel,
    ModelApiIdentity,
    ModelApiRuntime,
    TurnItem,
    TurnStream,
    content_text,
    message_fingerprint,
)
from coworker.core.ids import new_compact_id
from coworker.core.token_utils import estimate_text_tokens
from coworker.core.types import IncomingEvent
from coworker.i18n import tr

router = APIRouter()

_HEARTBEAT_SECONDS = 15.0
_MODEL_ID = "coworker"

_channel: ModelApiChannel | None = None
_runtime: ModelApiRuntime | None = None


def setup_model_api(
    *,
    channel: ModelApiChannel | None,
    runtime: ModelApiRuntime | None,
) -> None:
    """Wire the endpoint to the channel system; called once during startup.

    Person records are never created here: tokens are issued from the admin
    People page against an existing person, which is what binds the
    participant address to a persona.
    """
    global _channel, _runtime
    _channel = channel
    _runtime = runtime


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str = "user"
    content: str | list[Any] = ""
    tool_call_id: str | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = _MODEL_ID
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    tools: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/v1/models")
async def list_models(authorization: str | None = Header(default=None)) -> dict:
    _require_identity(authorization)
    return {
        "object": "list",
        "data": [
            {
                "id": _MODEL_ID,
                "object": "model",
                "created": 0,
                "owned_by": "coworker",
            }
        ],
    }


@router.post("/v1/chat/completions")
async def chat_completions(
    payload: ChatCompletionRequest,
    authorization: str | None = Header(default=None),
):
    identity = _require_identity(authorization)
    channel = _require_channel()
    runtime = _require_runtime()
    turns = runtime.turns

    fingerprints = [
        message_fingerprint(message.role, content_text(message.content))
        for message in payload.messages
    ]
    conversation_id, matched = runtime.sessions.match(
        identity.participant_id, fingerprints
    )
    turn = turns.get(identity.participant_id, conversation_id)
    delta = payload.messages[matched:]
    if not delta and turn is None:
        # The client resends a finished conversation with no new content
        # (a retry after the response was closed): redeliver the last message
        # so the agent can pick the conversation back up.
        delta = payload.messages[-1:]
    # System material is owned by the scenario event below; never double-
    # deliver it as a delta message. A mid-conversation scenario change is
    # re-injected through the scenario hash instead.
    delta = [message for message in delta if message.role != "system"]

    system_text = "\n".join(
        text
        for message in payload.messages
        if message.role == "system"
        for text in (content_text(message.content),)
        if text.strip()
    )
    scenario_hash = (
        hashlib.sha256(
            json.dumps(
                [system_text, payload.tools], ensure_ascii=False, default=str
            ).encode("utf-8")
        ).hexdigest()[:16]
        if system_text or payload.tools
        else ""
    )

    # Build the inbound events: the scenario notice first (only when new or
    # changed for this session), then each new caller message as its own
    # event. Large caller material is stored as a document; the notice tells
    # the model what it is, where it lives, and how to use it.
    events: list[IncomingEvent] = []
    if delta and runtime.sessions.scenario_changed(
        identity.participant_id, conversation_id, scenario_hash
    ):
        scenario = _render_scenario(runtime, scenario_hash, system_text, payload.tools)
        if scenario:
            events.append(
                IncomingEvent(
                    participant_id=identity.participant_id,
                    content=scenario,
                    conversation_id=conversation_id,
                    source="model_api",
                )
            )
    events.extend(
        IncomingEvent(
            participant_id=identity.participant_id,
            content=_render_delta_message(message),
            conversation_id=conversation_id,
            source="model_api",
        )
        for message in delta
    )

    if turn is None:
        turn = turns.open_or_get(identity.participant_id, conversation_id)
    queue = turn.attach()
    completion_id = new_compact_id(prefix="chatcmpl-")
    created = int(time.time())
    model = payload.model.strip() or _MODEL_ID

    try:
        for event in events:
            await channel.publish_inbound(event)
    except Exception:
        turn.detach(queue)
        raise

    if payload.stream:
        return StreamingResponse(
            _stream_events(turn, queue, completion_id, created, model),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    return await _collect_response(turn, queue, completion_id, created, model)


def _require_identity(authorization: str | None) -> ModelApiIdentity:
    runtime = _require_runtime()
    identity = runtime.directory.resolve_authorization(authorization)
    if identity is None:
        raise HTTPException(status_code=401, detail=tr("api.model_api.token_invalid"))
    return identity


def _require_channel() -> ModelApiChannel:
    if _channel is None:
        raise HTTPException(status_code=503, detail=tr("api.model_api.disabled"))
    return _channel


def _require_runtime() -> ModelApiRuntime:
    if _runtime is None or not _runtime.available:
        raise HTTPException(status_code=503, detail=tr("api.model_api.disabled"))
    return _runtime


_SCENARIO_EXCERPT_CHARS = 300


def _render_scenario(
    runtime: ModelApiRuntime,
    scenario_hash: str,
    system_text: str,
    tools: list[dict[str, Any]],
) -> str:
    """Store the caller material on disk and explain it to the model.

    The injected event stays small no matter how large the caller's system
    prompt or tool schemas are: it names the material's purpose, points at
    the stored document, excerpts the system prompt, lists the tool names,
    and spells out how tool invocation works.
    """
    document_path = runtime.scenarios.save(scenario_hash, system_text, tools)
    if document_path is None:
        return ""
    parts = [
        tr(
            "channel.model_api.scenario_header",
            hash=scenario_hash,
            path=str(document_path),
        )
    ]
    if system_text:
        parts.append(
            tr(
                "channel.model_api.scenario_prompt_note",
                chars=len(system_text),
                excerpt=_excerpt(system_text),
            )
        )
    names = _tool_names(tools)
    if names:
        parts.append(
            tr(
                "channel.model_api.scenario_tools_note",
                count=len(names),
                names=", ".join(names),
            )
        )
    return "\n".join(parts)


def _excerpt(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _SCENARIO_EXCERPT_CHARS:
        return collapsed
    return f"{collapsed[:_SCENARIO_EXCERPT_CHARS]}…"


def _tool_names(tools: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        name = (
            function.get("name")
            if isinstance(function, dict)
            else tool.get("name")
        )
        cleaned = str(name or "").strip()
        if cleaned:
            names.append(cleaned)
    return names


def _render_delta_message(message: ChatMessage) -> str:
    text = content_text(message.content)
    if message.role == "tool":
        return tr(
            "channel.model_api.caller_tool_result",
            tool_call_id=str(message.tool_call_id or ""),
            content=text,
        )
    if message.role == "assistant":
        return tr("channel.model_api.caller_assistant", content=text)
    if message.role == "system":
        return tr("channel.model_api.caller_system", content=text)
    return text


def _usage_estimates(turn: TurnStream, prompt_text: str) -> dict[str, int]:
    completion_tokens = estimate_text_tokens("\n".join(turn.texts))
    prompt_tokens = estimate_text_tokens(prompt_text)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _chunk(
    completion_id: str,
    created: int,
    model: str,
    delta: dict[str, Any] | None = None,
    *,
    finish_reason: str | None = None,
    usage: dict[str, int] | None = None,
    end_reason: str | None = None,
) -> str:
    body: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta or {},
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    if end_reason is not None:
        body["coworker_end_reason"] = end_reason
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


def _stream_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deltas: list[dict[str, Any]] = []
    for index, call in enumerate(tool_calls):
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            continue
        deltas.append(
            {
                "index": index,
                "id": str(call.get("id") or f"call_{index}"),
                "type": "function",
                "function": {
                    "name": str(function.get("name") or ""),
                    "arguments": str(function.get("arguments") or ""),
                },
            }
        )
    return deltas


async def _stream_events(
    turn: TurnStream,
    queue: asyncio.Queue[TurnItem],
    completion_id: str,
    created: int,
    model: str,
):
    try:
        yield _chunk(
            completion_id, created, model, delta={"role": "assistant", "content": ""}
        )
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if item.kind == "close":
                finish_reason = (
                    "tool_calls" if item.end_reason == "tool_calls" else "stop"
                )
                yield _chunk(
                    completion_id,
                    created,
                    model,
                    delta={},
                    finish_reason=finish_reason,
                    usage=item.usage or _usage_estimates(turn, ""),
                    end_reason=item.end_reason or "end_turn",
                )
                yield "data: [DONE]\n\n"
                return
            if item.text:
                yield _chunk(
                    completion_id, created, model, delta={"content": item.text}
                )
            if item.tool_calls:
                yield _chunk(
                    completion_id,
                    created,
                    model,
                    delta={"tool_calls": _stream_tool_calls(item.tool_calls)},
                )
    finally:
        turn.detach(queue)


async def _collect_response(
    turn: TurnStream,
    queue: asyncio.Queue[TurnItem],
    completion_id: str,
    created: int,
    model: str,
) -> dict[str, Any]:
    prompt_text = "\n".join(turn.texts)
    end_reason = "end_turn"
    while True:
        item = await queue.get()
        if item.kind == "close":
            end_reason = item.end_reason or "end_turn"
            break
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "\n\n".join(turn.texts),
    }
    finish_reason = "stop"
    if end_reason == "tool_calls" and turn.tool_calls:
        message["tool_calls"] = turn.tool_calls
        finish_reason = "tool_calls"
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": _usage_estimates(turn, prompt_text),
        "coworker_end_reason": end_reason,
    }
