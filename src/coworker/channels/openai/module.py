from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from coworker.channels.openai.channel import OpenAIChannel
from coworker.channels.openai.tokens import ExtraTokenStore
from coworker.core.config import APIConfig
from coworker.persona import PersonStore


@dataclass
class OpenAIModule:
    name = "openai"
    channel: OpenAIChannel
    extras: ExtraTokenStore
    management = None
    settings = None

    def attach_persist(
        self, persist: Callable[[dict[str, str]], Awaitable[None]]
    ) -> None:
        self.extras.set_persist(persist)

    def attach_person_store(self, store: PersonStore | None) -> None:
        self.channel.set_person_store(store)

    def attach_native_tool_names(self, names: set[str]) -> None:
        self.channel.set_native_tool_names(names)


def create_openai_module(
    api: APIConfig,
    *,
    person_store: PersonStore | None = None,
) -> OpenAIModule:
    extras = ExtraTokenStore(api.communication_tokens)
    channel = OpenAIChannel(
        extras=extras,
        person_store=person_store,
        timeout_seconds=float(api.compat_timeout_seconds),
    )
    return OpenAIModule(channel=channel, extras=extras)
