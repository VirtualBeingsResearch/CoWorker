"""Minimal AgentLoop + OpenAI-compat HTTP stack for end-to-end tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx

from coworker.agent.inbox_watcher import InboxWatcher
from coworker.agent.loop import AgentLoop
from coworker.api import app as api_app
from coworker.api.openai_compat import setup_openai_channel
from coworker.api.routes import setup as setup_routes
from coworker.brain.brain import Brain
from coworker.channels.openai.module import create_openai_module
from coworker.channels.system import create_channel_system
from coworker.core.config import APIConfig, Config
from coworker.core.types import AgentState
from coworker.memory.short_term import ShortTermMemory
from coworker.tools.client_tool import CallClientTool
from coworker.tools.communicate_tool import CommunicateTool
from coworker.tools.registry import ToolRegistry
from coworker.tools.system_tools import SleepTool
from tests.support.virtual_openai import (
    VIRTUAL_MODEL_ID,
    VIRTUAL_PROVIDER_NAME,
    VirtualOpenAIProvider,
    VirtualScenario,
)


@dataclass
class OpenAICompatHarness:
    client: httpx.AsyncClient
    provider: VirtualOpenAIProvider
    loop: AgentLoop
    token: str = "e2e-primary-token"


@asynccontextmanager
async def openai_compat_harness(
    tmp_path: Path,
    scenario: str | VirtualScenario | dict[str, Any] | None = None,
) -> AsyncIterator[OpenAICompatHarness]:
    config = Config(api=APIConfig(communication_token="e2e-primary-token", compat_timeout_seconds=8))
    config.agent.passive_mode = True
    config.agent.tick = False
    config.agent.inbox_dir = str(tmp_path / "inbox")
    config.agent.outbox_dir = str(tmp_path / "outbox")

    provider = VirtualOpenAIProvider(scenario=scenario)
    brain = Brain(VIRTUAL_PROVIDER_NAME, VIRTUAL_MODEL_ID, message_time_prefix=False)
    brain.register_provider(provider)

    inbox = InboxWatcher(config.agent.inbox_dir)
    channels = create_channel_system(config.agent.outbox_dir)
    channels.registry.set_inbound_handler(inbox.push)
    openai_module = create_openai_module(config.api)
    channels.install(openai_module)

    registry = ToolRegistry()
    registry.register_many(
        [
            CommunicateTool(channels.registry),
            CallClientTool(openai_module.channel),
            SleepTool(inbox, config=config),
        ]
    )
    openai_module.attach_native_tool_names(
        {name for name in registry.list_names() if name != "call_client_tool"}
    )

    prompt_builder = MagicMock()
    prompt_builder.build = MagicMock(return_value="system")
    prompt_builder.refresh = MagicMock()
    prompt_builder.consume_skill_load_warnings = MagicMock(return_value=[])
    long_term = MagicMock()
    long_term.is_ready = MagicMock(return_value=False)

    agent_loop = AgentLoop(
        brain=brain,
        short_term=ShortTermMemory(tree_enabled=False),
        long_term=long_term,
        tool_registry=registry,
        identity=MagicMock(name="e2e"),
        prompt_builder=prompt_builder,
        inbox_watcher=inbox,
        config=config,
        state=AgentState(
            current_provider=VIRTUAL_PROVIDER_NAME,
            current_model=VIRTUAL_MODEL_ID,
            tick=False,
        ),
    )

    api_app.set_setup_required(False)
    api_app._shutting_down = False
    setup_routes(
        inbox,
        agent_loop,
        brain,
        communication_token=config.api.communication_token,
        communication_token_explicit=True,
        channels=channels.registry,
    )
    setup_openai_channel(openai_module.channel)

    loop_task = asyncio.create_task(agent_loop.run(), name="openai-compat-e2e-loop")
    try:
        await _wait_until(lambda: agent_loop.state.is_sleeping, timeout=2)
        transport = httpx.ASGITransport(app=api_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield OpenAICompatHarness(client=client, provider=provider, loop=agent_loop)
    finally:
        agent_loop.stop()
        loop_task.cancel()
        try:
            await loop_task
        except (asyncio.CancelledError, Exception):
            pass
        setup_openai_channel(None)


async def _wait_until(predicate, *, timeout: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("timed out waiting for agent loop to rest")


def authorization(token: str = "e2e-primary-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def chat_payload(
    content: str,
    *,
    tools: list[dict[str, Any]] | None = None,
    conversation_id: str | None = None,
    extra_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "coworker",
        "messages": [{"role": "user", "content": content}, *(extra_messages or [])],
    }
    if tools is not None:
        body["tools"] = tools
    if conversation_id:
        body["conversation_id"] = conversation_id
    return body
