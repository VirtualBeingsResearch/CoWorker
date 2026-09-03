"""Virtual OpenAI provider driven by standard chat.completions fixtures.

Each case is an OpenAI ``request`` / ``response`` pair. The request is a partial
``POST /v1/chat/completions`` body used as a matcher. Fixture
``function.arguments`` may be a JSON object (preferred) or the wire-format
string; the HTTP response always stringifies arguments the way OpenAI does.
"""

from __future__ import annotations

import copy
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from coworker.brain.base import BaseLLMProvider
from coworker.core.constants import DEFAULT_LLM_MAX_TOKENS
from coworker.core.types import LLMResponse, Message, ToolCall

VIRTUAL_MODEL_ID = "virtual-model"
VIRTUAL_PROVIDER_NAME = "virtual"
CASES_DIR = Path(__file__).resolve().parent / "virtual_openai_cases"

InboundKind = Literal["user", "client_tools", "client_results", "native_tool", "idle"]

_HEADER_RE = re.compile(
    r"\[(?:from [^\]]+|来自[^\]]+)\]\[(?P<participant>[^\]]+)\]"
    r"(?:\[conversation:(?P<conversation>[^\]]+)\])?"
)
_CLIENT_RESULT_MARKERS = (
    "Client tool results for this window:",
    "本窗口的客户端工具结果：",
)
_USER_BODY_MARKERS = (" message:\n", "的消息:\n")
_RESULT_ITEM_RE = re.compile(
    r"(?P<call_id>\S+)\s*[（(](?P<name>[^）)]+)[）)]\s*[:：]\n(?P<content>.*?)(?=\n\S+\s*[（(]|\Z)",
    re.S,
)
_TEMPLATE_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")

SLEEP_RESPONSE: dict[str, Any] = {
    "id": "chatcmpl-sleep",
    "object": "chat.completion",
    "model": VIRTUAL_MODEL_ID,
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_sleep",
                        "type": "function",
                        "function": {"name": "sleep", "arguments": {"seconds": 0}},
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
}


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" or "text" in block:
                parts.append(str(block.get("text") or ""))
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return str(content)


def message_text(message: Message | Mapping[str, Any]) -> str:
    if isinstance(message, Message):
        return _content_text(message.content)
    return _content_text(message.get("content"))


def parse_inbound_header(text: str) -> tuple[str, str]:
    match = _HEADER_RE.search(text)
    if match is None:
        return "", ""
    return match.group("participant") or "", match.group("conversation") or ""


def inbound_user_payload(text: str) -> str:
    for marker in _USER_BODY_MARKERS:
        if marker in text:
            return text.split(marker, 1)[1].strip()
    return text.strip()


def _role(message: Message | Mapping[str, Any]) -> str:
    if isinstance(message, Message):
        return message.role
    return str(message.get("role") or "")


def _catalog_names(text: str) -> tuple[str, ...]:
    if "call_client_tool" not in text:
        return ()
    return tuple(re.findall(r'"name"\s*:\s*"([^"]+)"', text))


def _collect_catalog_names(messages: list[Message] | list[dict[str, Any]]) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for item in messages:
        if _role(item) != "user":
            continue
        for name in _catalog_names(message_text(item)):
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
    return tuple(names)


def _parse_client_results(text: str) -> tuple[tuple[str, str, str], ...]:
    body = text
    for marker in _CLIENT_RESULT_MARKERS:
        if marker in text:
            body = text.split(marker, 1)[1]
            break
    return tuple(
        (match.group("call_id"), match.group("name").strip(), match.group("content").strip())
        for match in _RESULT_ITEM_RE.finditer(body)
    )


def _is_client_results(text: str) -> bool:
    return any(marker in text for marker in _CLIENT_RESULT_MARKERS)


def openai_messages(messages: list[Message] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project Coworker messages into an OpenAI chat.completions message list."""

    return [{"role": _role(item), "content": message_text(item)} for item in messages]


def _tool_names(tools: Any) -> set[str]:
    names: set[str] = set()
    if not isinstance(tools, list):
        return names
    for item in tools:
        if not isinstance(item, dict):
            continue
        function = item.get("function") if item.get("type") == "function" else item
        if isinstance(function, dict):
            name = str(function.get("name") or "").strip()
            if name:
                names.add(name)
    return names


@dataclass(frozen=True)
class InboundSnapshot:
    kind: InboundKind
    participant_id: str = ""
    conversation_id: str = ""
    user_text: str = ""
    raw_text: str = ""
    client_tools: tuple[str, ...] = ()
    client_results: tuple[tuple[str, str, str], ...] = ()
    last_role: str = ""

    @property
    def client_result_text(self) -> str:
        if not self.client_results:
            return ""
        return "\n".join(item[2] for item in self.client_results)

    def substitutions(self) -> dict[str, str]:
        return {
            "participant_id": self.participant_id,
            "conversation_id": self.conversation_id,
            "user_text": self.user_text,
            "client_tool": self.client_tools[0] if self.client_tools else "",
            "client_result": self.client_result_text,
        }


def snapshot_messages(messages: list[Message] | list[dict[str, Any]]) -> InboundSnapshot:
    if not messages:
        return InboundSnapshot(kind="idle")
    last_role = _role(messages[-1])
    if last_role == "tool":
        return InboundSnapshot(kind="native_tool", last_role="tool")

    last_user: Message | dict[str, Any] | None = None
    for item in reversed(messages):
        if _role(item) == "user":
            last_user = item
            break
    if last_user is None:
        return InboundSnapshot(kind="idle", last_role=last_role)

    raw = message_text(last_user)
    participant_id, conversation_id = parse_inbound_header(raw)
    payload = inbound_user_payload(raw)
    user_text = payload.split("\n\n")[-1].strip() if payload else ""
    if _is_client_results(raw):
        return InboundSnapshot(
            kind="client_results",
            participant_id=participant_id,
            conversation_id=conversation_id,
            user_text=user_text,
            raw_text=raw,
            client_results=_parse_client_results(raw),
            last_role=last_role,
        )
    catalog = _collect_catalog_names(messages)
    if catalog:
        return InboundSnapshot(
            kind="client_tools",
            participant_id=participant_id,
            conversation_id=conversation_id,
            user_text=user_text,
            raw_text=raw,
            client_tools=catalog,
            last_role=last_role,
        )
    if participant_id.startswith("openai:"):
        return InboundSnapshot(
            kind="user",
            participant_id=participant_id,
            conversation_id=conversation_id,
            user_text=user_text,
            raw_text=raw,
            last_role=last_role,
        )
    return InboundSnapshot(kind="idle", raw_text=raw, last_role=last_role)


def request_matches(request: Mapping[str, Any], snapshot: InboundSnapshot) -> bool:
    """Match a partial OpenAI chat.completions request against the current turn."""

    messages = request.get("messages") or []
    if messages:
        wanted = messages[-1]
        if not isinstance(wanted, Mapping):
            return False
        role = str(wanted.get("role") or "user")
        if role == "tool":
            if snapshot.kind != "native_tool":
                return False
        elif snapshot.kind == "native_tool":
            return False
        content = wanted.get("content")
        if isinstance(content, str) and content:
            if content not in snapshot.user_text and content not in snapshot.raw_text:
                return False
    if "tools" in request:
        needed = _tool_names(request.get("tools"))
        have = set(snapshot.client_tools)
        if needed:
            if not needed.issubset(have):
                return False
        elif not have:
            return False
    elif snapshot.kind == "client_tools":
        return False
    return True


@dataclass(frozen=True)
class VirtualExchange:
    id: str
    request: dict[str, Any]
    response: dict[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> VirtualExchange:
        request = value.get("request")
        response = value.get("response")
        if not isinstance(request, dict):
            raise ValueError("exchange.request must be an OpenAI chat.completions request object")
        if not isinstance(response, dict) or not response.get("choices"):
            raise ValueError("exchange.response must be an OpenAI chat.completions response object")
        exchange_id = str(value.get("id") or response.get("id") or "")
        return cls(id=exchange_id, request=dict(request), response=dict(response))


@dataclass(frozen=True)
class VirtualScenario:
    id: str
    exchanges: tuple[VirtualExchange, ...]
    default_response: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(SLEEP_RESPONSE))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> VirtualScenario:
        if "turns" in value and "exchanges" not in value:
            raise ValueError(
                "virtual scenarios use OpenAI request/response `exchanges`, not `turns`"
            )
        raw_default = value.get("default_response")
        kwargs: dict[str, Any] = {
            "id": str(value.get("id") or "unnamed"),
            "exchanges": tuple(
                VirtualExchange.from_mapping(item) for item in value.get("exchanges") or ()
            ),
        }
        if isinstance(raw_default, dict) and raw_default.get("choices"):
            kwargs["default_response"] = dict(raw_default)
        return cls(**kwargs)

    @classmethod
    def from_json(cls, path: str | Path) -> VirtualScenario:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"scenario file must contain an object: {path}")
        return cls.from_mapping(payload)

    def select(self, snapshot: InboundSnapshot) -> VirtualExchange | None:
        for exchange in self.exchanges:
            if request_matches(exchange.request, snapshot):
                return exchange
        return None


def load_scenario(name: str) -> VirtualScenario:
    path = CASES_DIR / f"{name}.json"
    if not path.is_file():
        available = ", ".join(sorted(p.stem for p in CASES_DIR.glob("*.json"))) or "(none)"
        raise FileNotFoundError(f"unknown virtual scenario {name!r}; available: {available}")
    scenario = VirtualScenario.from_json(path)
    if scenario.id != name:
        raise ValueError(f"scenario id {scenario.id!r} does not match file stem {name!r}")
    return scenario


def resolve_scenario(scenario: str | VirtualScenario | Mapping[str, Any] | None) -> VirtualScenario:
    if scenario is None:
        return load_scenario("channel_compat")
    if isinstance(scenario, VirtualScenario):
        return scenario
    if isinstance(scenario, str):
        return load_scenario(scenario)
    return VirtualScenario.from_mapping(scenario)


def _fill_templates(value: Any, snapshot: InboundSnapshot) -> Any:
    if isinstance(value, str):
        mapping = snapshot.substitutions()

        def replace(match: re.Match[str]) -> str:
            return mapping.get(match.group(1), match.group(0))

        return _TEMPLATE_RE.sub(replace, value)
    if isinstance(value, dict):
        return {key: _fill_templates(item, snapshot) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill_templates(item, snapshot) for item in value]
    return value


def _encode_arguments(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    text = raw if isinstance(raw, str) else _encode_arguments(raw)
    try:
        parsed = json.loads(text) if text else {}
    except json.JSONDecodeError as error:
        return {"__parse_error__": str(error), "__raw_arguments__": text}
    return parsed if isinstance(parsed, dict) else {"__raw_arguments__": text}


def _with_routing(tool: str, arguments: dict[str, Any], snapshot: InboundSnapshot) -> dict[str, Any]:
    filled = dict(arguments)
    if tool in {"communicate", "call_client_tool"}:
        filled.setdefault("participant_id", snapshot.participant_id)
        if snapshot.conversation_id:
            filled.setdefault("conversation_id", snapshot.conversation_id)
    return filled


def _choice_message(response: Mapping[str, Any]) -> dict[str, Any]:
    choices = response.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return {}
    message = choices[0].get("message") or {}
    return message if isinstance(message, dict) else {}


def tool_calls_from_response(
    response: Mapping[str, Any],
    snapshot: InboundSnapshot,
) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, item in enumerate(_choice_message(response).get("tool_calls") or []):
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = str(function.get("name") or "")
        arguments = _with_routing(name, _parse_arguments(function.get("arguments")), snapshot)
        calls.append(
            ToolCall(
                id=str(item.get("id") or f"call_virtual_{index}"),
                name=name,
                arguments=arguments,
            )
        )
    return calls


def _write_arguments_back(response: dict[str, Any], calls: list[ToolCall]) -> None:
    raw_calls = _choice_message(response).get("tool_calls") or []
    for item, call in zip(raw_calls, calls, strict=False):
        if not isinstance(item, dict):
            continue
        function = item.setdefault("function", {})
        if isinstance(function, dict):
            function["arguments"] = _encode_arguments(call.arguments)


def materialize_response(
    response: Mapping[str, Any],
    snapshot: InboundSnapshot,
    *,
    model: str = VIRTUAL_MODEL_ID,
) -> tuple[dict[str, Any], list[ToolCall]]:
    body = _fill_templates(copy.deepcopy(dict(response)), snapshot)
    body.setdefault("object", "chat.completion")
    body.setdefault("model", model)
    body.setdefault("created", int(time.time()))
    body.setdefault("id", f"chatcmpl-{snapshot.kind}")
    calls = tool_calls_from_response(body, snapshot)
    _write_arguments_back(body, calls)
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        if calls:
            choices[0].setdefault("finish_reason", "tool_calls")
        else:
            choices[0].setdefault("finish_reason", "stop")
    return body, calls


@dataclass(frozen=True)
class TurnRecord:
    scenario_id: str
    turn_id: str
    snapshot: InboundSnapshot
    request: dict[str, Any]
    response: dict[str, Any]
    calls: tuple[ToolCall, ...]


class ScriptRunner:
    def __init__(self, scenario: VirtualScenario) -> None:
        self.scenario = scenario
        self.trace: list[TurnRecord] = []

    def complete(
        self,
        messages: list[Message] | list[dict[str, Any]],
        *,
        model: str = VIRTUAL_MODEL_ID,
        tools: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], list[ToolCall]]:
        snapshot = snapshot_messages(messages)
        request = {"model": model, "messages": openai_messages(messages)}
        if tools:
            request["tools"] = tools
        exchange = self.scenario.select(snapshot)
        turn_id = exchange.id if exchange is not None else "sleep"
        source = exchange.response if exchange is not None else self.scenario.default_response
        response, calls = materialize_response(source, snapshot, model=model)
        self.trace.append(
            TurnRecord(
                scenario_id=self.scenario.id,
                turn_id=turn_id,
                snapshot=snapshot,
                request=request,
                response=response,
                calls=tuple(calls),
            )
        )
        return response, calls

    def decide(self, messages: list[Message] | list[dict[str, Any]]) -> list[ToolCall]:
        _, calls = self.complete(messages)
        return calls

    @property
    def turn_ids(self) -> list[str]:
        return [record.turn_id for record in self.trace]

    @property
    def kinds(self) -> list[str]:
        return [record.snapshot.kind for record in self.trace]


def decide_virtual_turn(
    messages: list[Message] | list[dict[str, Any]],
    scenario: str | VirtualScenario | Mapping[str, Any] | None = None,
) -> list[ToolCall]:
    return ScriptRunner(resolve_scenario(scenario)).decide(messages)


class VirtualOpenAIProvider(BaseLLMProvider):
    """In-process stand-in that plays OpenAI chat.completions fixtures."""

    provider_type = ""
    provider_name = VIRTUAL_PROVIDER_NAME

    def __init__(
        self,
        name: str | None = None,
        scenario: str | VirtualScenario | Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(name or VIRTUAL_PROVIDER_NAME)
        self._current_model = VIRTUAL_MODEL_ID
        self.default_model = VIRTUAL_MODEL_ID
        self.runner = ScriptRunner(resolve_scenario(scenario))
        self.calls: list[list[Message]] = []

    @property
    def scenario(self) -> VirtualScenario:
        return self.runner.scenario

    @property
    def trace(self) -> list[TurnRecord]:
        return self.runner.trace

    @property
    def turn_ids(self) -> list[str]:
        return self.runner.turn_ids

    @property
    def kinds(self) -> list[str]:
        return self.runner.kinds

    async def complete(
        self,
        messages: list[Message],
        system_prompt: str,
        tools: list[dict],
        max_tokens: int = DEFAULT_LLM_MAX_TOKENS,
        thinking: bool = True,
        thinking_effort: str | None = None,
    ) -> LLMResponse:
        self.calls.append(list(messages))
        _, tool_calls = self.runner.complete(
            messages, model=self._current_model, tools=tools or None
        )
        return LLMResponse(
            content="",
            tool_calls=tool_calls,
            stop_reason="tool_use" if tool_calls else "end_turn",
            model=self._current_model,
            usage={"input_tokens": 8, "output_tokens": 4},
        )

    def set_model(self, model_id: str) -> None:
        self._current_model = model_id

    def list_models(self) -> list[str]:
        return [VIRTUAL_MODEL_ID]

    def supports_tool_use(self, model_id: str) -> bool:
        return True

    def supports_vision(self, model_id: str) -> bool:
        return False


class _ChatMessage(BaseModel):
    role: str
    content: Any = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class _ChatCompletionRequest(BaseModel):
    model: str = VIRTUAL_MODEL_ID
    messages: list[_ChatMessage] = Field(min_length=1)
    tools: list[dict[str, Any]] | None = None
    stream: bool = False


def create_virtual_openai_app(
    scenario: str | VirtualScenario | Mapping[str, Any] | None = None,
) -> FastAPI:
    """HTTP facade that returns fixture ``response`` objects."""

    runner = ScriptRunner(resolve_scenario(scenario))
    app = FastAPI(title="Coworker virtual OpenAI provider")
    app.state.runner = runner

    @app.get("/v1/models")
    async def list_models() -> dict[str, Any]:
        now = int(time.time())
        return {
            "object": "list",
            "data": [
                {
                    "id": VIRTUAL_MODEL_ID,
                    "object": "model",
                    "created": now,
                    "owned_by": "coworker-test",
                }
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(payload: _ChatCompletionRequest) -> Any:
        if payload.stream:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "streaming is not implemented on the virtual provider",
                        "type": "invalid_request_error",
                    }
                },
            )
        messages = [item.model_dump(exclude_none=True) for item in payload.messages]
        response, _ = runner.complete(
            messages,
            model=payload.model or VIRTUAL_MODEL_ID,
            tools=payload.tools,
        )
        return JSONResponse(response)

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_virtual_openai_app(), host="127.0.0.1", port=8765, log_level="info")
