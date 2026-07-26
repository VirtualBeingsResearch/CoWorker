from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from loguru import logger

from coworker.channels.weixin.client import DEFAULT_BASE_URL, WeixinCredentials
from coworker.i18n import tr


@dataclass(frozen=True)
class WeixinConnection:
    bot_instance_id: str
    token: str
    base_url: str = DEFAULT_BASE_URL
    weixin_user_id: str = ""
    display_name: str = ""
    enabled: bool = True

    @classmethod
    def from_credentials(cls, credentials: WeixinCredentials) -> WeixinConnection:
        return cls(
            bot_instance_id=credentials.bot_id,
            token=credentials.token,
            base_url=credentials.base_url,
            weixin_user_id=credentials.user_id,
        )


class WeixinConnectionRepository:
    """Persist connection resources owned by the Weixin channel."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._connections = self._load()

    def list(self) -> list[WeixinConnection]:
        return list(self._connections.values())

    async def save(self, connection: WeixinConnection) -> None:
        async with self._lock:
            existing = self._connections.get(connection.bot_instance_id)
            if existing is not None and not connection.display_name:
                connection = WeixinConnection(
                    bot_instance_id=connection.bot_instance_id,
                    token=connection.token,
                    base_url=connection.base_url,
                    weixin_user_id=connection.weixin_user_id,
                    display_name=existing.display_name,
                    enabled=existing.enabled,
                )
            self._connections[connection.bot_instance_id] = connection
            self._write()

    async def remove(self, bot_instance_id: str) -> bool:
        async with self._lock:
            if self._connections.pop(bot_instance_id, None) is None:
                return False
            self._write()
            return True

    async def update(
        self,
        bot_instance_id: str,
        *,
        display_name: str | None = None,
        enabled: bool | None = None,
    ) -> WeixinConnection | None:
        async with self._lock:
            current = self._connections.get(bot_instance_id)
            if current is None:
                return None
            updated = WeixinConnection(
                bot_instance_id=current.bot_instance_id,
                token=current.token,
                base_url=current.base_url,
                weixin_user_id=current.weixin_user_id,
                display_name=current.display_name if display_name is None else display_name,
                enabled=current.enabled if enabled is None else enabled,
            )
            self._connections[bot_instance_id] = updated
            self._write()
            return updated

    def _load(self) -> dict[str, WeixinConnection]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as error:
            logger.warning(tr("channel.weixin.connections_load_failed", error=error))
            return {}
        records = payload.get("connections") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            return {}
        connections: dict[str, WeixinConnection] = {}
        for record in records:
            connection = _connection_from_record(record)
            if connection is not None:
                connections[connection.bot_instance_id] = connection
        return connections

    def _write(self) -> None:
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(
                    {"connections": [asdict(connection) for connection in self._connections.values()]},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            temporary.replace(self._path)
        except OSError as error:
            logger.warning(tr("channel.weixin.connections_save_failed", error=error))
            temporary.unlink(missing_ok=True)
            raise


def _connection_from_record(record: object) -> WeixinConnection | None:
    if not isinstance(record, dict):
        return None
    bot_instance_id = str(record.get("bot_instance_id") or "").strip()
    token = str(record.get("token") or "")
    if not bot_instance_id or not token:
        return None
    return WeixinConnection(
        bot_instance_id=bot_instance_id,
        token=token,
        base_url=str(record.get("base_url") or DEFAULT_BASE_URL),
        weixin_user_id=str(record.get("weixin_user_id") or ""),
        display_name=str(record.get("display_name") or ""),
        enabled=record.get("enabled") is not False,
    )
