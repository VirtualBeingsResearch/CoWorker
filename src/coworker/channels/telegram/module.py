from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from coworker.channels.activity import ChannelActivityStore
from coworker.channels.telegram.channel import TelegramChannel
from coworker.channels.telegram.runner import TelegramRunner
from coworker.core.config import TelegramConfig
from coworker.i18n import tr


@dataclass(frozen=True)
class TelegramModuleResources:
    state_dir: Path
    attachments_dir: Path
    activity: ChannelActivityStore


@dataclass(frozen=True)
class TelegramModule:
    name = "telegram"

    channel: TelegramChannel
    runtime: TelegramRunner
    settings: TelegramSettings
    management: None = None


class TelegramSettings:
    config_key = "telegram"

    def __init__(self, runtime: TelegramRunner) -> None:
        self._runtime = runtime

    async def apply(self, config: object) -> None:
        if not isinstance(config, TelegramConfig):
            raise TypeError(tr("channel.telegram.config_type_invalid"))
        await self._runtime.reconfigure(config)


def create_telegram_module(
    config: TelegramConfig,
    resources: TelegramModuleResources,
) -> TelegramModule:
    runtime = TelegramRunner(
        config,
        resources.state_dir,
        resources.attachments_dir,
        resources.activity,
    )
    enabled = sum(
        bool(item.enabled and item.bot_token) for item in config.bots.values()
    )
    if enabled:
        logger.info(tr("channel.telegram.prepared", count=enabled))
    return TelegramModule(
        channel=TelegramChannel(runtime),
        runtime=runtime,
        settings=TelegramSettings(runtime),
    )
