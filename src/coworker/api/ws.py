from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
from pathlib import Path
from typing import Any

from fastapi import WebSocket
from loguru import logger

from coworker.core.types import CommunicateRequest
from coworker.i18n import tr

# 关闭哨兵：往出站队列塞这个值即可唤醒阻塞在 queue.get() 的 SSE/WS 发送循环，
# 让其立即跳出、释放连接，避免拖住 uvicorn 的优雅关闭。命名空间化，几乎不可能与真实消息撞。
SHUTDOWN_SENTINEL = "__coworker_shutdown__"
_MAX_ATTACHMENT_COUNT = 5
_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


def serialize_outbound_message(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, CommunicateRequest):
        payload = message.to_dict()
        if message.attachments:
            payload["attachments"] = _encode_attachments(message.attachments)
        return json.dumps(payload, ensure_ascii=False)
    return json.dumps(message, ensure_ascii=False)


def _encode_attachments(attachments: list[dict[str, Any]]) -> list[dict[str, str]]:
    if len(attachments) > _MAX_ATTACHMENT_COUNT:
        raise ValueError(tr("api.attachment.count_exceeded", limit=_MAX_ATTACHMENT_COUNT))
    encoded: list[dict[str, str]] = []
    for item in attachments:
        if not isinstance(item, dict):
            raise ValueError(tr("api.attachment.item_not_object"))
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError(tr("api.attachment.path_required"))
        path = Path(raw_path)
        if not path.is_file():
            raise ValueError(tr("api.attachment.missing", path=path))
        size = path.stat().st_size
        if size > _MAX_ATTACHMENT_BYTES:
            raise ValueError(
                tr(
                    "api.attachment.too_large",
                    name=path.name,
                    size=size,
                    limit=_MAX_ATTACHMENT_BYTES,
                )
            )
        filename = str(item.get("filename") or path.name)
        media_type = str(
            item.get("media_type")
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream"
        )
        encoded.append(
            {
                "filename": filename,
                "media_type": media_type,
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        )
    return encoded


class ConnectionPool:
    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._outboxes: dict[str, asyncio.Queue] = {}

    async def connect(
        self,
        participant_id: str,
        ws: WebSocket,
        queue: asyncio.Queue | None = None,
    ) -> asyncio.Queue:
        if participant_id in self._connections:
            raise ValueError(
                tr("api.attachment.participant_connected", participant=participant_id)
            )
        await ws.accept()
        self._connections[participant_id] = ws
        queue = queue or asyncio.Queue()
        self._outboxes[participant_id] = queue
        logger.info(f"WS connected: {participant_id}")
        return queue

    def disconnect(
        self,
        participant_id: str,
        ws: WebSocket | None = None,
        queue: asyncio.Queue | None = None,
    ) -> None:
        if ws is not None and self._connections.get(participant_id) is not ws:
            return
        if queue is not None and self._outboxes.get(participant_id) is not queue:
            return
        self._connections.pop(participant_id, None)
        self._outboxes.pop(participant_id, None)
        logger.info(f"WS disconnected: {participant_id}")

    def get_outbox(self, participant_id: str) -> asyncio.Queue | None:
        return self._outboxes.get(participant_id)

    def is_connected(
        self,
        participant_id: str,
        ws: WebSocket | None = None,
        queue: asyncio.Queue | None = None,
    ) -> bool:
        if participant_id not in self._connections:
            return False
        if ws is not None and self._connections.get(participant_id) is not ws:
            return False
        if queue is not None and self._outboxes.get(participant_id) is not queue:
            return False
        return True

    async def send(
        self,
        participant_id: str,
        message: Any,
        ws: WebSocket | None = None,
        queue: asyncio.Queue | None = None,
    ) -> None:
        ws = ws or self._connections.get(participant_id)
        if ws:
            try:
                await ws.send_text(serialize_outbound_message(message))
            except Exception as e:
                logger.warning(f"Failed to send WS message to {participant_id}: {e}")
                self.disconnect(participant_id, ws=ws, queue=queue)

    async def run_sender(
        self,
        participant_id: str,
        queue: asyncio.Queue,
        ws: WebSocket | None = None,
    ) -> None:
        while self.is_connected(participant_id, ws=ws, queue=queue):
            try:
                message = await asyncio.wait_for(queue.get(), timeout=1.0)
                if message == SHUTDOWN_SENTINEL:
                    break
                await self.send(participant_id, message, ws=ws, queue=queue)
            except TimeoutError:
                continue
            except Exception as e:
                logger.error(f"WS sender error for {participant_id}: {e}")
                break
