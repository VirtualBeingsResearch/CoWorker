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

from coworker.channels.activity import ChannelActivityStore
from coworker.channels.base import InboundHandler
from coworker.channels.weixin.client import (
    DEFAULT_BASE_URL,
    WeixinClient,
    credentials_from_login,
)
from coworker.channels.weixin.state import (
    WeixinAccountState,
    WeixinStateStore,
)
from coworker.core.types import IncomingEvent
from coworker.i18n import tr

if TYPE_CHECKING:
    from coworker.core.config import WeixinAccountConfig, WeixinConfig

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
    image_path: Path


class WeixinRunner:
    """Multi-account long-polling runtime for Tencent Weixin ClawBot."""

    name = "weixin"

    def __init__(
        self,
        config: WeixinConfig,
        state_path: Path,
        activity: ChannelActivityStore | None = None,
    ) -> None:
        self._config = config.model_copy(deep=True)
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

    @property
    def config(self) -> WeixinConfig:
        return self._config.model_copy(deep=True)

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        self._inbound_handler = handler

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

    async def send(self, participant_id: str, message: str) -> None:
        account_id, user_id = self._parse_participant(participant_id)
        account = self._account(account_id)
        state = self._account_state(account_id)
        client = self._clients.get(account_id)
        temporary = client is None
        client = client or WeixinClient(account.base_url, account.token)
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
            self._participant_id(account_id, participant_id)
            for account_id, state in self._state.accounts.items()
            if participant_id in state.context_tokens
        ]
        return matches[0] if len(matches) == 1 else None

    def participant_ids(self) -> list[str]:
        return [
            self._participant_id(account_id, user_id)
            for account_id, state in self._state.accounts.items()
            for user_id in state.context_tokens
        ]

    def is_account_active(self, account_id: str) -> bool:
        return account_id in self._active_accounts

    def activity_for(self, participant_id: str) -> tuple[str | None, str | None]:
        return self._activity.activity_for(participant_id)

    async def start_login(self) -> dict[str, str]:
        session_id = str(uuid4())
        client = WeixinClient(DEFAULT_BASE_URL)
        local_tokens = [
            account.token
            for account in self._config.accounts
            if account.token
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
            image_path=image_path,
        )
        image_data = base64.b64encode(image_bytes).decode()
        return {
            "session_id": session_id,
            "status": "wait",
            "qrcode_content": qrcode_content,
            "qrcode_data_url": f"data:image/png;base64,{image_data}",
            "qrcode_path": str(image_path),
        }

    async def poll_login(
        self,
        session_id: str,
        verify_code: str = "",
    ) -> dict[str, Any]:
        session = self._login_sessions.get(session_id)
        if session is None:
            raise RuntimeError("Weixin login session is unavailable")
        response = await session.client.poll_login(session.qrcode, verify_code)
        status = str(response.get("status") or "wait")
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
        for account in self._config.accounts:
            if not _account_is_configured(account):
                continue
            task = asyncio.create_task(
                self._run_account(account),
                name=f"weixin-account:{account.id}",
            )
            self._tasks[str(account.id)] = task

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
        await asyncio.gather(
            *(client.close() for client in clients),
            return_exceptions=True,
        )

    async def _run_account(self, account: WeixinAccountConfig) -> None:
        account_id = str(account.id)
        client = WeixinClient(account.base_url, account.token)
        self._clients[account_id] = client
        while True:
            try:
                await self._poll_account(account, client)
                self._active_accounts.add(account_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._active_accounts.discard(account_id)
                logger.warning(
                    tr(
                        "channel.weixin.poll_failed",
                        account=account_id,
                        error=error,
                    )
                )
                await asyncio.sleep(_RETRY_SECONDS)

    async def _poll_account(
        self,
        account: WeixinAccountConfig,
        client: WeixinClient,
    ) -> None:
        account_id = str(account.id)
        state = self._account_state(account_id)
        response = await client.get_updates(state.cursor)
        return_code = response.get("ret")
        if return_code not in (None, 0):
            raise RuntimeError(
                f"Weixin getupdates ret={return_code} "
                f"errcode={response.get('errcode')} errmsg={response.get('errmsg', '')}"
            )
        for message in response.get("msgs") or []:
            if isinstance(message, dict):
                await self._publish_message(account_id, message)
        state.cursor = str(response.get("get_updates_buf") or state.cursor)
        self._state_store.save(self._state)

    async def _publish_message(
        self,
        account_id: str,
        message: dict[str, Any],
    ) -> None:
        if message.get("message_type") not in (None, _MESSAGE_TYPE_USER):
            return
        user_id = str(message.get("from_user_id") or "").strip()
        if not user_id:
            return
        context_token = str(message.get("context_token") or "")
        if context_token:
            self._account_state(account_id).context_tokens[user_id] = context_token
        participant_id = self._participant_id(account_id, user_id)
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

    def _account(self, account_id: str) -> WeixinAccountConfig:
        for account in self._config.accounts:
            if str(account.id) == account_id and _account_is_configured(account):
                return account
        raise ValueError(f"Weixin account is unavailable: {account_id}")

    def _account_state(self, account_id: str) -> WeixinAccountState:
        return self._state.accounts.setdefault(account_id, WeixinAccountState())

    async def _redirect_login(self, session: _LoginSession, host: str) -> None:
        await session.client.close()
        session.client = WeixinClient(f"https://{host}")

    async def _close_login_session(self, session_id: str) -> None:
        session = self._login_sessions.pop(session_id, None)
        if session is None:
            return
        session.image_path.unlink(missing_ok=True)
        await session.client.close()

    async def _close_login_sessions(self) -> None:
        session_ids = list(self._login_sessions)
        await asyncio.gather(
            *(self._close_login_session(session_id) for session_id in session_ids),
            return_exceptions=True,
        )

    @staticmethod
    def _participant_id(account_id: str, user_id: str) -> str:
        return f"weixin:{account_id}:{user_id}"

    @staticmethod
    def _parse_participant(participant_id: str) -> tuple[str, str]:
        parts = participant_id.split(":", maxsplit=2)
        if len(parts) != 3 or parts[0] != "weixin" or not all(parts[1:]):
            raise ValueError(f"not a Weixin participant_id: {participant_id}")
        return parts[1], parts[2]


def _account_is_configured(account: WeixinAccountConfig) -> bool:
    return bool(account.enabled and account.bot_id and account.token)


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
