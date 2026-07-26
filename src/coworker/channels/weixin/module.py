from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from loguru import logger

from coworker.channels.activity import ChannelActivityStore
from coworker.channels.weixin.channel import WeixinChannel
from coworker.channels.weixin.connections import WeixinConnectionManager
from coworker.channels.weixin.repository import WeixinConnectionRepository
from coworker.channels.weixin.runner import WeixinRunner
from coworker.core.config import WeixinConfig
from coworker.i18n import tr


class WeixinModuleRuntime:
    """Expose one lifecycle for messaging and pairing internals."""

    name = "weixin"

    def __init__(
        self,
        messaging: WeixinRunner,
        connections: WeixinConnectionManager,
    ) -> None:
        self.messaging = messaging
        self.connections = connections

    async def start(self) -> None:
        await self.messaging.start()

    async def stop(self) -> None:
        await self.connections.stop()
        await self.messaging.stop()

    async def reconfigure(self, config: WeixinConfig) -> None:
        await self.messaging.reconfigure(config)


@dataclass(frozen=True)
class WeixinModule:
    name = "weixin"

    channel: WeixinChannel
    runtime: WeixinModuleRuntime
    management: WeixinManagement
    settings: WeixinSettings

    @property
    def connections(self) -> WeixinConnectionManager:
        return self.runtime.connections


class WeixinManagement:
    """Channel-owned management interface used by any administrative surface."""

    def __init__(self, connections: WeixinConnectionManager) -> None:
        self._connections = connections

    async def snapshot(self) -> dict[str, object]:
        return {
            "connections": [
                {
                    "bot_instance_id": connection.bot_instance_id,
                    "display_name": connection.display_name,
                    "enabled": connection.enabled,
                    "weixin_user_id": connection.weixin_user_id,
                }
                for connection in self._connections.list()
            ],
            "pairing": self._connections.current_pairing(),
        }

    async def execute(
        self,
        command: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        if command == "start_pairing":
            return cast(
                dict[str, object],
                await self._connections.start_pairing(),
            )
        if command == "verify_pairing":
            return await self._verify_pairing(payload)
        if command == "update_connection":
            return await self._update_connection(payload)
        if command == "remove_connection":
            return await self._remove_connection(payload)
        raise ValueError(tr("channel.management.command_unknown", command=command))

    async def _verify_pairing(
        self,
        payload: dict[str, object],
    ) -> dict[str, object]:
        session_id = _required_text(payload, "session_id")
        verify_code = _required_text(payload, "verify_code")
        return cast(
            dict[str, object],
            await self._connections.submit_verification(
                session_id,
                verify_code,
            ),
        )

    async def _update_connection(
        self,
        payload: dict[str, object],
    ) -> dict[str, object]:
        bot_instance_id = _required_text(payload, "bot_instance_id")
        display_name = payload.get("display_name")
        enabled = payload.get("enabled")
        connection = await self._connections.update(
            bot_instance_id,
            display_name=str(display_name) if display_name is not None else None,
            enabled=enabled if isinstance(enabled, bool) else None,
        )
        if connection is None:
            raise ValueError(
                tr("channel.weixin.connection_missing", instance=bot_instance_id)
            )
        return {"updated": True}

    async def _remove_connection(
        self,
        payload: dict[str, object],
    ) -> dict[str, object]:
        bot_instance_id = _required_text(payload, "bot_instance_id")
        if payload.get("confirm") is not True:
            raise ValueError(
                tr("channel.weixin.remove_confirm", instance=bot_instance_id)
            )
        if not await self._connections.remove(bot_instance_id):
            raise ValueError(
                tr("channel.weixin.connection_missing", instance=bot_instance_id)
            )
        return {"removed": True}


class WeixinSettings:
    config_key = "weixin"

    def __init__(self, runtime: WeixinModuleRuntime) -> None:
        self._runtime = runtime

    async def apply(self, config: object) -> None:
        if not isinstance(config, WeixinConfig):
            raise TypeError("Weixin settings require WeixinConfig")
        await self._runtime.reconfigure(config)


def create_weixin_module(
    config: WeixinConfig,
    data_dir: Path,
    activity: ChannelActivityStore,
) -> WeixinModule:
    repository = WeixinConnectionRepository(data_dir / "weixin_connections.json")
    connection_count = len(repository.list())
    if config.enabled and connection_count:
        logger.info(
            tr(
                "channel.weixin.prepared",
                count=connection_count,
            )
        )
    messaging = WeixinRunner(
        config,
        repository.list(),
        data_dir / "weixin_state.json",
        activity,
    )
    connections = WeixinConnectionManager(messaging, repository)
    runtime = WeixinModuleRuntime(messaging, connections)
    management = WeixinManagement(connections)
    settings = WeixinSettings(runtime)
    return WeixinModule(
        channel=WeixinChannel(runtime, messaging, connections),
        runtime=runtime,
        management=management,
        settings=settings,
    )


def _required_text(payload: dict[str, object], field: str) -> str:
    value = str(payload.get(field) or "").strip()
    if not value:
        raise ValueError(tr("channel.management.field_required", field=field))
    return value
