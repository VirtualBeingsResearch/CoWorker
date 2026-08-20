from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from coworker.channels.activity import ChannelActivityStore
from coworker.channels.wecom.channel import WeComChannel
from coworker.channels.wecom.runner import WeComRunner
from coworker.core.config import WeComConfig
from coworker.i18n import tr


@dataclass(frozen=True)
class WeComModuleResources:
    attachments_dir: Path
    contacts_path: Path
    activity: ChannelActivityStore


@dataclass(frozen=True)
class WeComModule:
    name = "wecom"

    channel: WeComChannel
    runtime: WeComRunner
    settings: WeComSettings
    management: None = None


class WeComSettings:
    config_key = "wecom"

    def __init__(self, runtime: WeComRunner) -> None:
        self._runtime = runtime

    async def apply(self, config: object) -> None:
        if not isinstance(config, WeComConfig):
            raise TypeError(tr("channel.wecom.config_type_invalid"))
        await self._runtime.reconfigure(config)


def create_wecom_module(
    config: WeComConfig,
    resources: WeComModuleResources,
) -> WeComModule:
    runtime = WeComRunner(
        cfg=config,
        attachments_dir=resources.attachments_dir,
        contacts_path=resources.contacts_path,
        activity=resources.activity,
    )
    ready = sum(1 for bot in config.bots.values() if bot.enabled and bot.bot_id and bot.secret)
    if ready:
        logger.info(tr("channel.wecom.prepared", count=ready))
    return WeComModule(
        channel=WeComChannel(runtime),
        runtime=runtime,
        settings=WeComSettings(runtime),
    )
