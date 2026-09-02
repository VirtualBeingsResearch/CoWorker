"""OpenAI-compatible inbound HTTP: /v1/models and /v1/chat/completions."""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from coworker.api.routes import (
    is_authenticated_relay_request,
    openai_participant_id,
)
from coworker.channels.openai.channel import (
    OpenAIChannel,
    catalog_from_tools,
    fingerprint_conversation,
    first_system_text,
    last_user_text,
)
from coworker.channels.openai.tokens import ExtraTokenStore
from coworker.channels.openai.waiters import BusyError, OpenAICompletion
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


def _authorize(request: Request, authorization: str | None) -> str:
    if is_authenticated_relay_request(request):
        raise OpenAIHTTPError(
            403,
            tr("api.openai.relay_forbidden"),
            "relay_forbidden",
        )
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
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _authorize(request, authorization)
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
    request: Request,
    authorization: str | None = Header(default=None),
    x_coworker_conversation_id: str | None = Header(default=None),
) -> Any:
    participant_id = _authorize(request, authorization)
    channel = _channel()
    messages = [item.model_dump(exclude_none=True) for item in payload.messages]
    conversation_id = _resolve_conversation_id(
        payload,
        x_coworker_conversation_id,
        messages,
    )
    if _is_tool_followup(messages):
        try:
            completion = await channel.open_tool_followup(
                participant_id=participant_id,
                conversation_id=conversation_id,
                results=_tool_results(messages),
            )
        except BusyError as error:
            _raise_busy(error.reason)
        except ValueError as error:
            raise OpenAIHTTPError(400, str(error)) from error
    else:
        user_text = last_user_text(messages)
        if not user_text:
            raise OpenAIHTTPError(400, tr("api.openai.user_message_required"))
        if channel.sessions().awaiting_tools(participant_id, conversation_id):
            _raise_busy("tools")
        originating = _originating_user_text(messages)
        try:
            completion = await channel.open_user_turn(
                participant_id=participant_id,
                conversation_id=conversation_id,
                user_text=user_text,
                system_text=first_system_text(messages),
                catalog=catalog_from_tools(payload.tools),
                originating_task=originating,
            )
        except BusyError as error:
            _raise_busy(error.reason)
    body = _completion_body(payload.model or _MODEL_ID, completion)
    if payload.stream:
        return StreamingResponse(
            _sse(body),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    return JSONResponse(body)


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
) -> str:
    for candidate in (
        (header or "").strip(),
        (payload.conversation_id or "").strip(),
    ):
        if candidate:
            return candidate
    return fingerprint_conversation(messages)


def _is_tool_followup(messages: list[dict[str, Any]]) -> bool:
    if not messages:
        return False
    return str(messages[-1].get("role") or "") == "tool"


def _tool_results(messages: list[dict[str, Any]]) -> dict[str, str]:
    trailing: list[dict[str, Any]] = []
    for item in reversed(messages):
        if str(item.get("role") or "") != "tool":
            break
        trailing.append(item)
    results: dict[str, str] = {}
    for item in reversed(trailing):
        call_id = str(item.get("tool_call_id") or "").strip()
        if not call_id:
            continue
        results[call_id] = last_user_text(
            [{"role": "user", "content": item.get("content")}]
        )
    if not results:
        raise ValueError(tr("api.openai.tool_results_missing"))
    return results


def _originating_user_text(messages: list[dict[str, Any]]) -> str:
    for item in messages:
        if str(item.get("role") or "") == "user":
            return last_user_text([item])
    return ""


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


async def _sse(body: dict[str, Any]):
    yield f"data: {json.dumps(body, ensure_ascii=False)}\n\n"
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
