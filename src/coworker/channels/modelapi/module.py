"""Model API channel module: settings hot-reload for the admin console."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from coworker.channels.modelapi.channel import ModelApiChannel
from coworker.channels.modelapi.runtime import ModelApiRuntime
from coworker.channels.modelapi.turns import TurnWatchdogRuntime
from coworker.core.config import ModelApiConfig
from coworker.i18n import tr


class ModelApiSettings:
    config_key = "model_api"

    def __init__(self, runtime: ModelApiRuntime) -> None:
        self._runtime = runtime

    async def apply(self, config: object) -> None:
        if not isinstance(config, ModelApiConfig):
            raise TypeError(tr("channel.model_api.config_type_invalid"))
        self._runtime.reconfigure(config)


@dataclass(frozen=True)
class ModelApiModule:
    name = "model-api"

    channel: ModelApiChannel
    runtime: ModelApiRuntime
    settings: ModelApiSettings
    management: None = None


def create_model_api_module(
    config: ModelApiConfig,
    sessions_path: str | Path,
) -> ModelApiModule:
    runtime = ModelApiRuntime(config, sessions_path)
    return ModelApiModule(
        channel=ModelApiChannel(
            runtime,
            watchdog=TurnWatchdogRuntime(runtime.turns),
        ),
        runtime=runtime,
        settings=ModelApiSettings(runtime),
    )
