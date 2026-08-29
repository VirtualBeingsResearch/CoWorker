"""OpenAI-compatible model API channel package."""

from __future__ import annotations

from coworker.channels.modelapi.channel import ModelApiChannel
from coworker.channels.modelapi.module import (
    ModelApiModule,
    ModelApiSettings,
    create_model_api_module,
)
from coworker.channels.modelapi.runtime import ModelApiRuntime
from coworker.channels.modelapi.sessions import (
    ModelApiIdentity,
    ModelApiTokenDirectory,
    SessionMatcher,
    SessionRecord,
    content_text,
    message_fingerprint,
)
from coworker.channels.modelapi.turns import (
    TurnItem,
    TurnRegistry,
    TurnStream,
    TurnWatchdogRuntime,
)

__all__ = [
    "ModelApiChannel",
    "ModelApiIdentity",
    "ModelApiModule",
    "ModelApiRuntime",
    "ModelApiSettings",
    "ModelApiTokenDirectory",
    "SessionMatcher",
    "SessionRecord",
    "TurnItem",
    "TurnRegistry",
    "TurnStream",
    "TurnWatchdogRuntime",
    "content_text",
    "create_model_api_module",
    "message_fingerprint",
]
