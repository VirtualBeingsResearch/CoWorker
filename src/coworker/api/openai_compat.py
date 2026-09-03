"""OpenAI-compatible inbound HTTP: /v1/models and /v1/chat/completions."""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from coworker.api.routes import openai_participant_id
from coworker.channels.openai.channel import (
    OpenAIChannel,
    catalog_from_tools,
    first_system_text,
    image_attachments_from_message,
    image_attachments_from_messages,
    last_user_image_attachments,
    last_user_text,
    tool_call_ids_from_messages,
    turn_user_text,
)
from coworker.channels.openai.tokens import ExtraTokenStore
from coworker.channels.openai.waiters import BusyError, OpenAICompletion, OpenAITurn
from coworker.core.ids import new_compact_id
from coworker.i18n import tr

router = APIRouter()

_openai_channel: OpenAIChannel | None = None
_MODEL_ID = "coworker"


class OpenAIHTTPError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


def setup_openai_channel(channel: OpenAIChannel | None) -> None:
    global _openai_channel
    _openai_channel = channel


def get_openai_channel() -> OpenAIChannel | None:
    return _openai_channel


def sync_extra_token_store(tokens: dict[str, str]) -> None:
    if _openai_channel is None:
        return
    _openai_channel.extras().replace(tokens)


def extra_token_store() -> ExtraTokenStore | None:
    if _openai_channel is None:
        return None
    return _openai_channel.extras()


def _channel() -> OpenAIChannel:
    if _openai_channel is None:
        raise OpenAIHTTPError(
            503,
            tr("api.state.channel_runtime_not_ready"),
            "not_ready",
        )
    return _openai_channel


def openai_error_body(
    message: str,
    *,
    error_type: str = "invalid_request_error",
    code: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"error": {"message": message, "type": error_type}}
    if code:
        body["error"]["code"] = code
    return body


def openai_json_response(
    status: int,
    message: str,
    *,
    code: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=openai_error_body(message, code=code),
    )


def setup_required_v1_response() -> JSONResponse:
    return openai_json_response(
        503,
        tr("api.openai.setup_required"),
        code="setup_required",
    )


class ChatMessage(BaseModel):
    role: str
    content: Any = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    model: str = _MODEL_ID
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    conversation_id: str | None = None
    user: str | None = None
    temperature: float | None = None
    tool_choice: Any = None


def _authorize(authorization: str | None) -> str:
    try:
        return openai_participant_id(authorization)
    except HTTPException as error:
        code = "invalid_api_key" if error.status_code == 401 else None
        if error.status_code == 503:
            code = "unconfigured"
        raise OpenAIHTTPError(
            error.status_code,
            str(error.detail),
            code,
        ) from error


@router.get("/v1/models")
async def list_models(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _authorize(authorization)
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": _MODEL_ID,
                "object": "model",
                "created": now,
                "owned_by": "coworker",
            }
        ],
    }


@router.post("/v1/chat/completions")
async def chat_completions(
    payload: ChatCompletionRequest,
    authorization: str | None = Header(default=None),
    x_coworker_conversation_id: str | None = Header(default=None),
) -> Any:
    participant_id = _authorize(authorization)
    channel = _channel()
    messages = [item.model_dump(exclude_none=True) for item in payload.messages]
    conversation_id = _resolve_conversation_id(
        payload,
        x_coworker_conversation_id,
        messages,
        channel=channel,
        participant_id=participant_id,
    )
    model = payload.model or _MODEL_ID
    stream_headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    pending = channel.sessions().pending_tool_turn(participant_id, conversation_id)
    if _is_tool_followup(messages) or _is_image_user_followup(messages, pending):
        try:
            results, image_dicts = _followup_results(messages, pending)
            attachments = channel.materialize_image_dicts(image_dicts)
        except ValueError as error:
            raise OpenAIHTTPError(400, str(error)) from error
        if payload.stream:
            try:
                turn = await channel.start_tool_followup(
                    participant_id=participant_id,
                    conversation_id=conversation_id,
                    results=results,
                    stream=True,
                    attachments=attachments,
                )
            except BusyError as error:
                _raise_busy(error.reason)
            except ValueError as error:
                raise OpenAIHTTPError(400, str(error)) from error
            return StreamingResponse(
                _sse_turn(model, channel, turn),
                media_type="text/event-stream",
                headers=stream_headers,
            )
        try:
            completion = await channel.open_tool_followup(
                participant_id=participant_id,
                conversation_id=conversation_id,
                results=results,
                attachments=attachments,
            )
        except BusyError as error:
            _raise_busy(error.reason)
        except ValueError as error:
            raise OpenAIHTTPError(400, str(error)) from error
        return JSONResponse(_completion_body(model, completion))

    user_text = turn_user_text(messages)
    try:
        attachments = channel.materialize_user_images(messages)
    except ValueError as error:
        raise OpenAIHTTPError(400, str(error)) from error
    if not user_text and not attachments:
        raise OpenAIHTTPError(400, tr("api.openai.user_message_required"))
    if channel.sessions().awaiting_tools(participant_id, conversation_id):
        _raise_busy("tools")
    catalog = catalog_from_tools(payload.tools)
    system_text = first_system_text(messages)
    if payload.stream:
        try:
            turn = await channel.start_user_turn(
                participant_id=participant_id,
                conversation_id=conversation_id,
                user_text=user_text,
                system_text=system_text,
                catalog=catalog,
                attachments=attachments,
                stream=True,
            )
        except BusyError as error:
            _raise_busy(error.reason)
        return StreamingResponse(
            _sse_turn(model, channel, turn),
            media_type="text/event-stream",
            headers=stream_headers,
        )
    try:
        completion = await channel.open_user_turn(
            participant_id=participant_id,
            conversation_id=conversation_id,
            user_text=user_text,
            system_text=system_text,
            catalog=catalog,
            attachments=attachments,
        )
    except BusyError as error:
        _raise_busy(error.reason)
    return JSONResponse(_completion_body(model, completion))


def _raise_busy(reason: str) -> None:
    message = (
        tr("api.openai.busy_tools")
        if reason == "tools"
        else tr("api.openai.busy_turn")
    )
    raise OpenAIHTTPError(409, message, "conflict")


def _resolve_conversation_id(
    payload: ChatCompletionRequest,
    header: str | None,
    messages: list[dict[str, Any]],
    *,
    channel: OpenAIChannel,
    participant_id: str,
) -> str:
    for candidate in (
        (header or "").strip(),
        (payload.conversation_id or "").strip(),
    ):
        if candidate:
            return candidate
    return channel.resolve_implicit_conversation_id(participant_id, messages)


def _is_tool_followup(messages: list[dict[str, Any]]) -> bool:
    if not messages:
        return False
    return str(messages[-1].get("role") or "") == "tool"


def _is_image_user_followup(messages: list[dict[str, Any]], pending: OpenAITurn | None) -> bool:
    if pending is None or not messages:
        return False
    last = messages[-1]
    if str(last.get("role") or "") != "user":
        return False
    pending_ids = {item.openai_id for item in pending.pending_calls()}
    if not pending_ids or not (pending_ids & tool_call_ids_from_messages(messages)):
        return False
    return bool(image_attachments_from_message(last))


def _trailing_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trailing: list[dict[str, Any]] = []
    for item in reversed(messages):
        if str(item.get("role") or "") != "tool":
            break
        trailing.append(item)
    trailing.reverse()
    return trailing


def _tool_results(messages: list[dict[str, Any]]) -> dict[str, str]:
    results: dict[str, str] = {}
    for item in _trailing_tool_messages(messages):
        call_id = str(item.get("tool_call_id") or "").strip()
        if not call_id:
            continue
        results[call_id] = last_user_text(
            [{"role": "user", "content": item.get("content")}]
        )
    if not results:
        raise ValueError(tr("api.openai.tool_results_missing"))
    return results


def _followup_results(
    messages: list[dict[str, Any]],
    pending: OpenAITurn | None,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    if _is_tool_followup(messages):
        trailing = _trailing_tool_messages(messages)
        return _tool_results(messages), image_attachments_from_messages(trailing)
    if pending is None:
        raise ValueError(tr("api.openai.tool_followup_unexpected"))
    results = {
        item.openai_id: last_user_text(messages) for item in pending.pending_calls()
    }
    if not results:
        raise ValueError(tr("api.openai.tool_results_missing"))
    return results, last_user_image_attachments(messages)


def _completion_body(model: str, completion: OpenAICompletion) -> dict[str, Any]:
    created = int(time.time())
    completion_id = f"chatcmpl-{new_compact_id()}"
    message: dict[str, Any]
    if completion.timed_out:
        message = {
            "role": "assistant",
            "content": tr("api.openai.timeout"),
        }
        finish = "stop"
    elif completion.kind == "tool_calls":
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments,
                    },
                }
                for call in completion.tool_calls
            ],
        }
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": completion.content or ""}
        finish = "stop"
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model or _MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish,
            }
        ],
    }


def _chunk_body(
    *,
    completion_id: str,
    created: int,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None,
) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model or _MODEL_ID,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


async def _sse_turn(model: str, channel: OpenAIChannel, turn: OpenAITurn):
    completion_id = f"chatcmpl-{new_compact_id()}"
    created = int(time.time())
    role_sent = False
    try:
        async for event in turn.iter_events():
            if event.kind == "delta":
                delta: dict[str, Any] = {}
                if not role_sent:
                    delta["role"] = "assistant"
                    role_sent = True
                if event.content:
                    delta["content"] = event.content
                if not delta:
                    continue
                yield (
                    "data: "
                    + json.dumps(
                        _chunk_body(
                            completion_id=completion_id,
                            created=created,
                            model=model,
                            delta=delta,
                            finish_reason=None,
                        ),
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
            elif event.kind == "timeout":
                delta = {}
                if not role_sent:
                    delta["role"] = "assistant"
                    role_sent = True
                delta["content"] = tr("api.openai.timeout")
                yield (
                    "data: "
                    + json.dumps(
                        _chunk_body(
                            completion_id=completion_id,
                            created=created,
                            model=model,
                            delta=delta,
                            finish_reason=None,
                        ),
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                yield (
                    "data: "
                    + json.dumps(
                        _chunk_body(
                            completion_id=completion_id,
                            created=created,
                            model=model,
                            delta={},
                            finish_reason="stop",
                        ),
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
            elif event.kind == "tool_calls":
                delta = {}
                if not role_sent:
                    delta["role"] = "assistant"
                    role_sent = True
                delta["tool_calls"] = [
                    {
                        "index": index,
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    }
                    for index, call in enumerate(event.tool_calls)
                ]
                yield (
                    "data: "
                    + json.dumps(
                        _chunk_body(
                            completion_id=completion_id,
                            created=created,
                            model=model,
                            delta=delta,
                            finish_reason=None,
                        ),
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                yield (
                    "data: "
                    + json.dumps(
                        _chunk_body(
                            completion_id=completion_id,
                            created=created,
                            model=model,
                            delta={},
                            finish_reason="tool_calls",
                        ),
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
            elif event.kind == "stop":
                yield (
                    "data: "
                    + json.dumps(
                        _chunk_body(
                            completion_id=completion_id,
                            created=created,
                            model=model,
                            delta={},
                            finish_reason="stop",
                        ),
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
    finally:
        channel.settle_turn(turn)
    yield "data: [DONE]\n\n"


__all__ = [
    "OpenAIHTTPError",
    "extra_token_store",
    "get_openai_channel",
    "openai_json_response",
    "router",
    "setup_openai_channel",
    "setup_required_v1_response",
    "sync_extra_token_store",
]
