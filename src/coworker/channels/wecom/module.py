from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from coworker.channels.activity import ChannelActivityStore
from coworker.channels.wecom.channel import WeComChannel
from coworker.channels.wecom.runner import WeComRunner
from coworker.core.config import WeComConfig


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
            raise TypeError("WeCom settings require WeComConfig")
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
    if config.enabled and not (config.bot_id and config.secret):
        logger.warning(
            "WeCom enabled but bot_id/secret missing; runtime is waiting for configuration"
        )
    elif config.enabled:
        logger.info(f"WeCom runner prepared, bot_id={config.bot_id}")
    return WeComModule(
        channel=WeComChannel(runtime),
        runtime=runtime,
        settings=WeComSettings(runtime),
    )
