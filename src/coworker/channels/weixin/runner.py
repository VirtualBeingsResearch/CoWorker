from __future__ import annotations

import asyncio
import base64
import io
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import qrcode
from loguru import logger

from coworker.channels.access import (
    ChannelAccessController,
    inbound_access_denied_message,
)
from coworker.channels.activity import ChannelActivityStore
from coworker.channels.base import InboundHandler
from coworker.channels.weixin.client import (
    DEFAULT_BASE_URL,
    WeixinClient,
    credentials_from_login,
)
from coworker.channels.weixin.logging import configure_weixin_polling_logs
from coworker.channels.weixin.repository import WeixinConnection
from coworker.channels.weixin.state import (
    WeixinConnectionState,
    WeixinStateStore,
)
from coworker.core.types import IncomingEvent
from coworker.i18n import tr

if TYPE_CHECKING:
    from coworker.core.config import WeixinConfig

_MESSAGE_TYPE_USER = 1
_TEXT_ITEM_TYPE = 1
_IMAGE_ITEM_TYPE = 2
_VOICE_ITEM_TYPE = 3
_FILE_ITEM_TYPE = 4
_VIDEO_ITEM_TYPE = 5
_RETRY_SECONDS = 3.0
_TERMINAL_LOGIN_STATUSES = {"expired", "verify_code_blocked", "binded_redirect"}


@dataclass
class _LoginSession:
    client: WeixinClient
    qrcode: str
    qrcode_content: str
    image_path: Path
    status: str = "wait"


class WeixinRunner:
    """Multi-account long-polling runtime for Tencent Weixin ClawBot."""

    name = "weixin"

    def __init__(
        self,
        config: WeixinConfig,
        connections: list[WeixinConnection],
        state_path: Path,
        activity: ChannelActivityStore | None = None,
    ) -> None:
        configure_weixin_polling_logs()
        self._config = config.model_copy(deep=True)
        self._connections = {
            connection.bot_instance_id: connection for connection in connections
        }
        self._state_store = WeixinStateStore(state_path)
        self._state_path = state_path
        self._state = self._state_store.load()
        self._activity = activity or ChannelActivityStore()
        self._inbound_handler: InboundHandler | None = None
        self._clients: dict[str, WeixinClient] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._active_accounts: set[str] = set()
        self._login_sessions: dict[str, _LoginSession] = {}
        self._login_lock = asyncio.Lock()
        self._polling_failures: set[str] = set()
        self._access = ChannelAccessController()

    @property
    def config(self) -> WeixinConfig:
        return self._config.model_copy(deep=True)

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        self._inbound_handler = handler

    def set_access_controller(self, access: ChannelAccessController) -> None:
        self._access = access

    async def start(self) -> None:
        while not self._stop.is_set():
            await self._replace_account_tasks()
            self._wake.clear()
            await self._wake.wait()
        await self._cancel_account_tasks()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        await self._close_login_sessions()

    async def reconfigure(self, config: WeixinConfig) -> None:
        if self._config == config:
            return
        self._config = config.model_copy(deep=True)
        await self._cancel_account_tasks()
        self._wake.set()

    async def replace_connections(self, connections: list[WeixinConnection]) -> None:
        next_connections = {
            connection.bot_instance_id: connection for connection in connections
        }
        if self._connections == next_connections:
            return
        restart_required = _runtime_connections(self._connections) != _runtime_connections(
            next_connections
        )
        self._connections = next_connections
        self._prune_removed_connection_state()
        if restart_required:
            await self._cancel_account_tasks()
            self._wake.set()

    async def send(self, participant_id: str, message: str) -> None:
        bot_instance_id = self._parse_participant(participant_id)
        connection = self._connection(bot_instance_id)
        state = self._connection_state(bot_instance_id)
        user_id = connection.weixin_user_id or _only_user_id(state)
        if not user_id:
            raise ValueError(f"Weixin recipient is unavailable: {bot_instance_id}")
        client = self._clients.get(bot_instance_id)
        temporary = client is None
        client = client or WeixinClient(connection.base_url, connection.token)
        try:
            await client.send_text(
                user_id,
                message,
                state.context_tokens.get(user_id, ""),
            )
        finally:
            if temporary:
                await client.close()
        self._activity.record_sent(participant_id)

    def resolve_participant(self, participant_id: str) -> str | None:
        matches = [
            self._participant_id(connection.bot_instance_id)
            for connection in self._connections.values()
            if _connection_is_available(connection)
            and participant_id
            in {connection.bot_instance_id, connection.display_name}
        ]
        return matches[0] if len(matches) == 1 else None

    def participant_ids(self) -> list[str]:
        return [
            self._participant_id(connection.bot_instance_id)
            for connection in self._connections.values()
            if _connection_is_available(connection)
        ]

    def is_instance_active(self, bot_instance_id: str) -> bool:
        self._connection(bot_instance_id)
        return bot_instance_id in self._active_accounts

    def instance_name(self, bot_instance_id: str) -> str:
        connection = self._connection(bot_instance_id)
        return connection.display_name or bot_instance_id

    def activity_for(self, participant_id: str) -> tuple[str | None, str | None]:
        return self._activity.activity_for(participant_id)

    async def start_login(self) -> dict[str, str]:
        async with self._login_lock:
            session_id = str(uuid4())
            client = WeixinClient(DEFAULT_BASE_URL)
            local_tokens = [
                connection.token
                for connection in self._connections.values()
                if connection.token
            ]
            response = await client.start_login(local_tokens)
            qrcode_value = str(response.get("qrcode") or "")
            qrcode_content = str(response.get("qrcode_img_content") or "")
            if not qrcode_value or not qrcode_content:
                await client.close()
                raise RuntimeError("Weixin login response did not include a QR code")
            image_bytes = _render_qrcode(qrcode_content)
            image_path = self._state_path.with_name(f"weixin-pairing-{session_id}.png")
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(image_bytes)
            self._login_sessions[session_id] = _LoginSession(
                client=client,
                qrcode=qrcode_value,
                qrcode_content=qrcode_content,
                image_path=image_path,
            )
            await self._close_login_sessions(except_session_id=session_id)
            return self._login_snapshot(session_id, self._login_sessions[session_id])

    def current_login(self) -> dict[str, str] | None:
        if not self._login_sessions:
            return None
        session_id, session = next(reversed(self._login_sessions.items()))
        return self._login_snapshot(session_id, session)

    async def poll_login(
        self,
        session_id: str,
        verify_code: str = "",
    ) -> dict[str, Any]:
        async with self._login_lock:
            session = self._login_sessions.get(session_id)
            if session is None:
                raise RuntimeError("Weixin login session is unavailable")
            response = await session.client.poll_login(session.qrcode, verify_code)
            status = str(response.get("status") or "wait")
            session.status = status
            if status == "scaned_but_redirect" and response.get("redirect_host"):
                await self._redirect_login(session, str(response["redirect_host"]))
            result = {
                "status": status,
                "credentials": credentials_from_login(response),
            }
            if result["credentials"] is not None or status in _TERMINAL_LOGIN_STATUSES:
                await self._close_login_session(session_id)
            return result

    async def _replace_account_tasks(self) -> None:
        await self._cancel_account_tasks()
        if not self._config.enabled:
            return
        for connection in self._connections.values():
            if not _connection_is_available(connection):
                continue
            task = asyncio.create_task(
                self._run_connection(connection),
                name=f"weixin-account:{connection.bot_instance_id}",
            )
            self._tasks[connection.bot_instance_id] = task

    async def _cancel_account_tasks(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        clients = list(self._clients.values())
        self._clients.clear()
        self._active_accounts.clear()
        self._polling_failures.clear()
        await asyncio.gather(
            *(client.close() for client in clients),
            return_exceptions=True,
        )

    async def _run_connection(self, connection: WeixinConnection) -> None:
        bot_instance_id = connection.bot_instance_id
        client = WeixinClient(connection.base_url, connection.token)
        self._clients[bot_instance_id] = client
        while True:
            try:
                await self._poll_connection(connection, client)
                self._active_accounts.add(bot_instance_id)
                if bot_instance_id in self._polling_failures:
                    self._polling_failures.remove(bot_instance_id)
                    logger.info(
                        tr(
                            "channel.weixin.poll_recovered",
                            account=bot_instance_id,
                        )
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._active_accounts.discard(bot_instance_id)
                log = (
                    logger.debug
                    if bot_instance_id in self._polling_failures
                    else logger.warning
                )
                log(
                    tr(
                        "channel.weixin.poll_failed",
                        account=bot_instance_id,
                        error=error,
                    )
                )
                self._polling_failures.add(bot_instance_id)
                await asyncio.sleep(_RETRY_SECONDS)

    async def _poll_connection(
        self,
        connection: WeixinConnection,
        client: WeixinClient,
    ) -> None:
        bot_instance_id = connection.bot_instance_id
        state = self._connection_state(bot_instance_id)
        response = await client.get_updates(state.cursor)
        return_code = response.get("ret")
        if return_code not in (None, 0):
            raise RuntimeError(
                f"Weixin getupdates ret={return_code} "
                f"errcode={response.get('errcode')} errmsg={response.get('errmsg', '')}"
            )
        for message in response.get("msgs") or []:
            if isinstance(message, dict):
                await self._publish_message(bot_instance_id, message, client)
        state.cursor = str(response.get("get_updates_buf") or state.cursor)
        self._state_store.save(self._state)

    async def _publish_message(
        self,
        bot_instance_id: str,
        message: dict[str, Any],
        client: WeixinClient,
    ) -> None:
        if message.get("message_type") not in (None, _MESSAGE_TYPE_USER):
            return
        user_id = str(message.get("from_user_id") or "").strip()
        if not user_id:
            return
        participant_id = self._participant_id(bot_instance_id)
        if not self._access.allows("weixin", "inbound", participant_id):
            logger.info(
                tr(
                    "channel.access.inbound_denied",
                    channel="weixin",
                    participant=participant_id,
                )
            )
            self._access.traffic.record(
                direction="inbound",
                channel="weixin",
                participant_id=participant_id,
                status="denied",
                source="weixin",
                reason="policy",
            )
            try:
                await client.send_text(
                    user_id,
                    inbound_access_denied_message(),
                    str(message.get("context_token") or ""),
                )
                self._access.traffic.record(
                    direction="outbound",
                    channel="weixin",
                    participant_id=participant_id,
                    status="sent",
                    source="access_policy",
                    reason="rejection_notice",
                )
            except Exception as error:
                self._access.traffic.record(
                    direction="outbound",
                    channel="weixin",
                    participant_id=participant_id,
                    status="failed",
                    source="access_policy",
                    reason="rejection_notice",
                )
                logger.warning(
                    tr(
                        "channel.access.inbound_denied_reply_failed",
                        channel="weixin",
                        participant=participant_id,
                        error=error,
                    )
                )
            return
        context_token = str(message.get("context_token") or "")
        if context_token:
            self._connection_state(bot_instance_id).context_tokens[user_id] = context_token
        self._activity.record_received(participant_id)
        if self._inbound_handler is None:
            logger.warning(tr("channel.weixin.inbound_unhandled"))
            return
        await self._inbound_handler(
            IncomingEvent(
                participant_id=participant_id,
                content=_message_text(message),
                conversation_id=str(message.get("session_id") or "") or None,
                source="weixin",
                event_id=str(message.get("message_id") or "") or None,
            )
        )

    def _connection(self, bot_instance_id: str) -> WeixinConnection:
        connection = self._connections.get(bot_instance_id)
        if connection is not None and _connection_is_available(connection):
            return connection
        raise ValueError(f"Weixin Bot instance is unavailable: {bot_instance_id}")

    def _connection_state(self, bot_instance_id: str) -> WeixinConnectionState:
        return self._state.connections.setdefault(
            bot_instance_id,
            WeixinConnectionState(),
        )

    async def _redirect_login(self, session: _LoginSession, host: str) -> None:
        await session.client.close()
        session.client = WeixinClient(f"https://{host}")

    async def _close_login_session(self, session_id: str) -> None:
        session = self._login_sessions.pop(session_id, None)
        if session is None:
            return
        session.image_path.unlink(missing_ok=True)
        await session.client.close()

    async def _close_login_sessions(self, except_session_id: str = "") -> None:
        session_ids = [
            session_id
            for session_id in self._login_sessions
            if session_id != except_session_id
        ]
        await asyncio.gather(
            *(self._close_login_session(session_id) for session_id in session_ids),
            return_exceptions=True,
        )

    @staticmethod
    def _participant_id(bot_instance_id: str) -> str:
        return f"weixin:{bot_instance_id}"

    @staticmethod
    def _parse_participant(participant_id: str) -> str:
        parts = participant_id.split(":", maxsplit=1)
        if len(parts) != 2 or parts[0] != "weixin" or not parts[1]:
            raise ValueError(f"not a Weixin participant_id: {participant_id}")
        return parts[1]

    def _login_snapshot(
        self,
        session_id: str,
        session: _LoginSession,
    ) -> dict[str, str]:
        image_data = base64.b64encode(session.image_path.read_bytes()).decode()
        return {
            "session_id": session_id,
            "status": session.status,
            "qrcode_content": session.qrcode_content,
            "qrcode_data_url": f"data:image/png;base64,{image_data}",
            "qrcode_path": str(session.image_path),
        }

    def _prune_removed_connection_state(self) -> None:
        removed_ids = self._state.connections.keys() - self._connections.keys()
        for bot_instance_id in removed_ids:
            self._state.connections.pop(bot_instance_id, None)
            self._polling_failures.discard(bot_instance_id)
        if removed_ids:
            self._state_store.save(self._state)


def _connection_is_available(connection: WeixinConnection) -> bool:
    return bool(connection.enabled and connection.bot_instance_id and connection.token)


def _runtime_connections(
    connections: dict[str, WeixinConnection],
) -> dict[str, tuple[str, str, str, bool]]:
    return {
        bot_instance_id: (
            connection.token,
            connection.base_url,
            connection.weixin_user_id,
            connection.enabled,
        )
        for bot_instance_id, connection in connections.items()
    }


def _only_user_id(state: WeixinConnectionState) -> str:
    return next(iter(state.context_tokens)) if len(state.context_tokens) == 1 else ""


def _message_text(message: dict[str, Any]) -> str:
    labels = {
        _IMAGE_ITEM_TYPE: tr("channel.weixin.image"),
        _FILE_ITEM_TYPE: tr("channel.weixin.file"),
        _VIDEO_ITEM_TYPE: tr("channel.weixin.video"),
    }
    parts: list[str] = []
    for item in message.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == _TEXT_ITEM_TYPE:
            text = str((item.get("text_item") or {}).get("text") or "")
            if text:
                parts.append(text)
        elif item_type == _VOICE_ITEM_TYPE:
            transcript = str((item.get("voice_item") or {}).get("text") or "")
            parts.append(transcript or tr("channel.weixin.voice"))
        elif item_type in labels:
            parts.append(labels[item_type])
    return "\n".join(parts) or tr("channel.weixin.unsupported")


def _render_qrcode(content: str) -> bytes:
    image = qrcode.make(content)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
