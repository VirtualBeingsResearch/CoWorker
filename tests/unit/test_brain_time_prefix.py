from __future__ import annotations

import re
from datetime import datetime

import pytest

from coworker.brain.base import BaseLLMProvider
from coworker.brain.brain import Brain
from coworker.core.types import LLMResponse, Message
from coworker.i18n import locale_context

_PREFIX_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} 周[一二三四五六日]\] ")


class _CapturingProvider(BaseLLMProvider):
    provider_name = "fake"

    def __init__(self) -> None:
        self.seen: list[Message] = []

    async def complete(
        self, messages, system_prompt, tools, max_tokens=4096, thinking=True
    ) -> LLMResponse:
        self.seen = messages
        return LLMResponse(
            content="ok", tool_calls=[], stop_reason="end_turn", model="fake", usage={}
        )

    def set_model(self, model_id: str) -> None:  # noqa: D401
        pass

    def list_models(self) -> list[str]:
        return ["fake"]

    def supports_tool_use(self, model_id: str) -> bool:
        return True

    def supports_vision(self, model_id: str) -> bool:
        return True


def _make_brain(provider: _CapturingProvider, *, prefix: bool) -> Brain:
    brain = Brain("fake", "fake", message_time_prefix=prefix)
    brain.register_provider(provider)
    return brain


def _messages() -> list[Message]:
    ts = datetime(2026, 6, 1, 14, 30, 5)  # 2026-06-01 is a Monday -> 周一
    return [
        Message(role="user", content="hello", timestamp=ts),
        Message(role="assistant", content="hi there", timestamp=ts),
        Message(role="tool", content="tool output", tool_call_id="t1", timestamp=ts),
    ]


@pytest.mark.asyncio
async def test_user_message_gets_timestamp_prefix() -> None:
    provider = _CapturingProvider()
    brain = _make_brain(provider, prefix=True)

    await brain.think(_messages(), "sys", [])

    user_msg = provider.seen[0]
    assert user_msg.role == "user"
    assert _PREFIX_RE.match(user_msg.content)
    assert user_msg.content == "[2026-06-01 14:30:05 周一] hello"


@pytest.mark.asyncio
async def test_english_locale_uses_english_weekday() -> None:
    provider = _CapturingProvider()
    brain = _make_brain(provider, prefix=True)

    with locale_context("en"):
        await brain.think(_messages(), "sys", [])

    assert provider.seen[0].content == "[2026-06-01 14:30:05 Monday] hello"


@pytest.mark.asyncio
async def test_assistant_and_tool_unchanged() -> None:
    provider = _CapturingProvider()
    brain = _make_brain(provider, prefix=True)

    await brain.think(_messages(), "sys", [])

    assert provider.seen[1].content == "hi there"
    assert provider.seen[2].content == "tool output"


@pytest.mark.asyncio
async def test_original_messages_not_mutated() -> None:
    provider = _CapturingProvider()
    brain = _make_brain(provider, prefix=True)

    original = _messages()
    await brain.think(original, "sys", [])

    assert original[0].content == "hello"  # untouched copy semantics


@pytest.mark.asyncio
async def test_disabled_adds_no_prefix() -> None:
    provider = _CapturingProvider()
    brain = _make_brain(provider, prefix=False)

    await brain.think(_messages(), "sys", [])

    assert provider.seen[0].content == "hello"


@pytest.mark.asyncio
async def test_content_blocks_get_prefix_block() -> None:
    provider = _CapturingProvider()
    brain = _make_brain(provider, prefix=True)

    ts = datetime(2026, 6, 1, 14, 30, 5)
    msg = Message(
        role="user",
        content=[{"type": "text", "text": "see image"}],
        timestamp=ts,
    )
    await brain.think([msg], "sys", [])

    blocks = provider.seen[0].content
    assert blocks[0] == {"type": "text", "text": "[2026-06-01 14:30:05 周一]"}
    assert blocks[1] == {"type": "text", "text": "see image"}
    # original untouched
    assert msg.content == [{"type": "text", "text": "see image"}]
