from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from coworker.channels.weixin.client import WeixinCredentials
from coworker.channels.weixin.repository import (
    WeixinConnection,
    WeixinConnectionRepository,
)
from coworker.channels.weixin.runner import WeixinRunner
from coworker.i18n import tr

_PAIRING_POLL_SECONDS = 0.9
_TERMINAL_PAIRING_STATUSES = {
    "confirmed",
    "expired",
    "verify_code_blocked",
    "binded_redirect",
}


class WeixinConnectionManager:
    """Own durable connections and the active Weixin pairing lifecycle."""

    def __init__(
        self,
        runtime: WeixinRunner,
        repository: WeixinConnectionRepository,
    ) -> None:
        self._runtime = runtime
        self._repository = repository
        self._pairing: dict[str, str] | None = None
        self._pairing_task: asyncio.Task[None] | None = None
        self._poll_lock = asyncio.Lock()

    def list(self) -> list[WeixinConnection]:
        return self._repository.list()

    async def start_pairing(self) -> dict[str, str]:
        if not self._runtime.config.enabled:
            raise RuntimeError(tr("channel.weixin.disabled"))
        await self._cancel_pairing_task()
        self._pairing = await self._runtime.start_login()
        session_id = self._pairing["session_id"]
        self._pairing_task = asyncio.create_task(
            self._watch_pairing(session_id),
            name=f"weixin-pairing:{session_id}",
        )
        return dict(self._pairing)

    def current_pairing(self) -> dict[str, str] | None:
        return dict(self._pairing) if self._pairing is not None else None

    async def submit_verification(
        self,
        session_id: str,
        verify_code: str,
    ) -> dict[str, str]:
        if not verify_code:
            raise ValueError(tr("channel.weixin.verify_code_required"))
        await self._poll_pairing(session_id, verify_code)
        return self._pairing_snapshot(session_id)

    async def remove(self, bot_instance_id: str) -> bool:
        removed = await self._repository.remove(bot_instance_id)
        if removed:
            await self._runtime.replace_connections(self._repository.list())
        return removed

    async def update(
        self,
        bot_instance_id: str,
        *,
        display_name: str | None = None,
        enabled: bool | None = None,
    ) -> WeixinConnection | None:
        connection = await self._repository.update(
            bot_instance_id,
            display_name=display_name,
            enabled=enabled,
        )
        if connection is not None:
            await self._runtime.replace_connections(self._repository.list())
        return connection

    async def stop(self) -> None:
        await self._cancel_pairing_task()

    async def _watch_pairing(self, session_id: str) -> None:
        while not self._pairing_finished(session_id):
            try:
                await self._poll_pairing(session_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.debug(
                    tr(
                        "channel.weixin.pairing_poll_failed",
                        session=session_id,
                        error=error,
                    )
                )
            if not self._pairing_finished(session_id):
                await asyncio.sleep(_PAIRING_POLL_SECONDS)

    async def _poll_pairing(
        self,
        session_id: str,
        verify_code: str = "",
    ) -> None:
        async with self._poll_lock:
            if self._pairing_finished(session_id):
                return
            result = await self._runtime.poll_login(session_id, verify_code)
            await self._apply_pairing_result(session_id, result)

    async def _apply_pairing_result(
        self,
        session_id: str,
        result: dict[str, Any],
    ) -> None:
        pairing = self._require_pairing(session_id)
        status = str(result.get("status") or "wait")
        pairing["status"] = status
        credentials = result.get("credentials")
        if not isinstance(credentials, WeixinCredentials):
            return
        connection = WeixinConnection.from_credentials(credentials)
        await self._repository.save(connection)
        await self._runtime.replace_connections(self._repository.list())
        pairing.update(
            {
                "status": "confirmed",
                "participant_id": f"weixin:{connection.bot_instance_id}",
                "bot_instance_id": connection.bot_instance_id,
                "weixin_user_id": connection.weixin_user_id,
            }
        )

    def _pairing_finished(self, session_id: str) -> bool:
        if self._pairing is None or self._pairing.get("session_id") != session_id:
            return True
        return self._pairing.get("status") in _TERMINAL_PAIRING_STATUSES

    def _pairing_snapshot(self, session_id: str) -> dict[str, str]:
        return dict(self._require_pairing(session_id))

    def _require_pairing(self, session_id: str) -> dict[str, str]:
        if self._pairing is None or self._pairing.get("session_id") != session_id:
            raise RuntimeError(tr("channel.weixin.pairing_unavailable"))
        return self._pairing

    async def _cancel_pairing_task(self) -> None:
        task = self._pairing_task
        self._pairing_task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
