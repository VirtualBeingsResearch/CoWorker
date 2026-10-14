from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import secrets
import tempfile
from dataclasses import dataclass
from functools import partial
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
from coworker.channels.progress import attachment_display_name
from coworker.channels.weixin.client import (
    DEFAULT_BASE_URL,
    DEFAULT_CDN_BASE_URL,
    WeixinClient,
    WeixinDeliveryError,
    credentials_from_login,
)
from coworker.channels.weixin.logging import configure_weixin_polling_logs
from coworker.channels.weixin.media import (
    WeixinMediaTooLargeError,
    aes_ecb_padded_size,
    encode_outbound_aes_key,
    encrypt_aes_ecb_file,
    extension_for_mime,
    format_size,
    guess_file_mime,
    resolve_media_key,
    safe_filename,
    sniff_image_mime,
)
from coworker.channels.weixin.repository import WeixinConnection
from coworker.channels.weixin.state import (
    WeixinConnectionState,
    WeixinStateStore,
)
from coworker.core.ids import new_compact_id
from coworker.core.types import AttachmentData, IncomingEvent
from coworker.i18n import bind_locale, tr

if TYPE_CHECKING:
    from coworker.core.config import WeixinConfig

_MESSAGE_TYPE_USER = 1
_TEXT_ITEM_TYPE = 1
_IMAGE_ITEM_TYPE = 2
_VOICE_ITEM_TYPE = 3
_FILE_ITEM_TYPE = 4
_VIDEO_ITEM_TYPE = 5
_UPLOAD_MEDIA_TYPES = {"image": 1, "video": 2, "file": 3}
_HASH_CHUNK_BYTES = 1024 * 1024
_RETRY_SECONDS = 3.0
_TERMINAL_LOGIN_STATUSES = {"expired", "verify_code_blocked", "binded_redirect"}
_INLINE_BASE64_LIMIT = 10 * 1024 * 1024


@dataclass
class _LoginSession:
    client: WeixinClient
    qrcode: str
    qrcode_content: str
    image_path: Path
    status: str = "wait"


@dataclass(frozen=True)
class _MediaRef:
    """Downloadable media extracted from one inbound message item."""

    item_type: int
    encrypt_query_param: str = ""
    full_url: str = ""
    aeskey_hex: str = ""
    media_aes_key: str = ""
    filename: str = ""


@dataclass(frozen=True)
class _PreparedUpload:
    path: Path
    filename: str
    declared: str
    media_type: str


@dataclass(frozen=True)
class WeixinOutboundStart:
    """What communicate already finished before attachment uploads continue."""

    text_sent: bool
    started: tuple[str, ...]
    rejected: tuple[tuple[str, str], ...]


class WeixinRunner:
    """Multi-account long-polling runtime for Tencent Weixin ClawBot."""

    name = "weixin"

    def __init__(
        self,
        config: WeixinConfig,
        connections: list[WeixinConnection],
        state_path: Path,
        activity: ChannelActivityStore | None = None,
        attachments_dir: Path | None = None,
    ) -> None:
        configure_weixin_polling_logs()
        self._config = config.model_copy(deep=True)
        self._connections = {
            connection.bot_instance_id: connection for connection in connections
        }
        self._state_store = WeixinStateStore(state_path)
        self._state_path = state_path
        self._state = self._state_store.load()
        self._attachments_dir = attachments_dir or state_path.parent / "attachments"
        self._activity = activity or ChannelActivityStore()
        self._inbound_handler: InboundHandler | None = None
        self._clients: dict[str, WeixinClient] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._upload_tasks: set[asyncio.Task[None]] = set()
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
        await self._cancel_upload_tasks()
        await self._close_login_sessions()

    async def drain_uploads(self) -> None:
        """Wait until attachment uploads scheduled by :meth:`send` finish."""
        while self._upload_tasks:
            await asyncio.gather(*tuple(self._upload_tasks))

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

    async def send(
        self,
        participant_id: str,
        message: str,
        attachments: list[dict[str, Any]] | None = None,
        *,
        conversation_id: str | None = None,
    ) -> WeixinOutboundStart:
        bot_instance_id = self._parse_participant(participant_id)
        connection = self._connection(bot_instance_id)
        state = self._connection_state(bot_instance_id)
        user_id = connection.weixin_user_id or _only_user_id(state)
        if not user_id:
            raise ValueError(f"Weixin recipient is unavailable: {bot_instance_id}")
        context_token = state.context_tokens.get(user_id, "")
        existing = self._clients.get(bot_instance_id)
        owns_client = existing is None
        client = existing or WeixinClient(connection.base_url, connection.token)
        try:
            text_sent = False
            if message.strip():
                await client.send_text(user_id, message, context_token)
                text_sent = True
                self._activity.record_sent(participant_id)
            prepared: list[_PreparedUpload] = []
            rejected: list[tuple[str, str]] = []
            for attachment in attachments or []:
                try:
                    prepared.append(self._prepare_upload(attachment))
                except Exception as error:
                    rejected.append(
                        (
                            attachment_display_name(attachment),
                            _safe_delivery_error(error),
                        )
                    )
            if prepared:
                self._schedule_upload(
                    client,
                    owns_client=owns_client,
                    participant_id=participant_id,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    context_token=context_token,
                    prepared=tuple(prepared),
                    timeout=float(self._config.upload_timeout_seconds),
                )
                owns_client = False
            return WeixinOutboundStart(
                text_sent=text_sent,
                started=tuple(item.filename for item in prepared),
                rejected=tuple(rejected),
            )
        finally:
            if owns_client:
                await client.close()

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

    async def _cancel_upload_tasks(self) -> None:
        tasks = tuple(self._upload_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._upload_tasks.clear()

    async def _cancel_account_tasks(self) -> None:
        await self._cancel_upload_tasks()
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
        content = _message_text(message)
        attachments: list[AttachmentData] = []
        for ref in _media_refs(message):
            try:
                attachments.append(await self._download_media(client, ref))
            except WeixinMediaTooLargeError as error:
                content = (
                    f"{content}\n"
                    f"{tr(
                        'channel.weixin.attachment_skipped',
                        filename=ref.filename,
                        size=format_size(error.size),
                        limit=format_size(error.limit),
                    )}"
                )
            except Exception as error:
                logger.warning(
                    tr(
                        "channel.weixin.download_failed",
                        account=bot_instance_id,
                        filename=ref.filename,
                        error=error,
                    )
                )
                content = (
                    f"{content}\n"
                    f"{tr('channel.weixin.attachment_unavailable', filename=ref.filename)}"
                )
        await self._inbound_handler(
            IncomingEvent(
                participant_id=participant_id,
                content=content,
                conversation_id=str(message.get("session_id") or "") or None,
                source="weixin",
                attachments=attachments,
                event_id=str(message.get("message_id") or "") or None,
            )
        )

    async def _download_media(
        self,
        client: WeixinClient,
        ref: _MediaRef,
    ) -> AttachmentData:
        self._attachments_dir.mkdir(parents=True, exist_ok=True)
        stem = safe_filename(ref.filename)
        destination = self._reserve_attachment_path(stem)
        try:
            size = await client.download_media(
                destination,
                encrypt_query_param=ref.encrypt_query_param,
                aes_key=resolve_media_key(ref.aeskey_hex, ref.media_aes_key),
                full_url=ref.full_url,
                max_bytes=self._config.max_download_mb * 1024 * 1024,
            )
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        if ref.item_type == _IMAGE_ITEM_TYPE:
            media_type = sniff_image_mime(destination.read_bytes())
            suffix = extension_for_mime(media_type)
            filename = f"{stem}{suffix}"
            final_path = destination.with_name(f"{destination.name}{suffix}")
            if not final_path.exists():
                destination.rename(final_path)
                destination = final_path
        elif ref.item_type == _FILE_ITEM_TYPE:
            media_type = guess_file_mime(stem)
            filename = stem
        else:
            media_type = "video/mp4"
            filename = stem
        inline = size <= _INLINE_BASE64_LIMIT and (
            media_type.startswith("image/") or media_type == "application/pdf"
        )
        return AttachmentData(
            filename=filename,
            media_type=media_type,
            saved_path=str(destination),
            data=base64.b64encode(destination.read_bytes()).decode("ascii")
            if inline
            else None,
        )

    def _prepare_upload(self, attachment: dict[str, Any]) -> _PreparedUpload:
        declared = _declared_attachment_type(attachment)
        path = _attachment_path(attachment)
        limit = self._config.max_upload_mb * 1024 * 1024
        declared_size = path.stat().st_size
        if declared_size > limit:
            raise ValueError(
                tr(
                    "channel.weixin.attachment_too_large",
                    filename=path.name,
                    size=declared_size,
                    limit=limit,
                )
            )
        return _PreparedUpload(
            path=path,
            filename=safe_filename(str(attachment.get("filename") or path.name)),
            declared=declared,
            media_type=str(attachment.get("media_type") or ""),
        )

    def _schedule_upload(
        self,
        client: WeixinClient,
        *,
        owns_client: bool,
        participant_id: str,
        conversation_id: str | None,
        user_id: str,
        context_token: str,
        prepared: tuple[_PreparedUpload, ...],
        timeout: float,
    ) -> None:
        task = asyncio.create_task(
            bind_locale(
                partial(
                    self._upload_batch,
                    client,
                    owns_client=owns_client,
                    participant_id=participant_id,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    context_token=context_token,
                    prepared=prepared,
                    timeout=timeout,
                )
            ),
            name=f"weixin-upload-{participant_id}",
        )
        self._upload_tasks.add(task)
        task.add_done_callback(self._upload_tasks.discard)

    async def _upload_batch(
        self,
        client: WeixinClient,
        *,
        owns_client: bool,
        participant_id: str,
        conversation_id: str | None,
        user_id: str,
        context_token: str,
        prepared: tuple[_PreparedUpload, ...],
        timeout: float,
    ) -> None:
        try:
            announced: list[_PreparedUpload] = []
            for item in prepared:
                notice = tr(
                    "channel.weixin.upload_started_notice",
                    filename=item.filename,
                )
                try:
                    await client.send_text(user_id, notice, context_token)
                    self._activity.record_sent(participant_id)
                except Exception as error:
                    failed = tr(
                        "channel.weixin.upload_failed_notice",
                        filename=item.filename,
                        error=_safe_delivery_error(error),
                    )
                    await self._push_upload_status(
                        participant_id,
                        conversation_id,
                        failed,
                    )
                    continue
                announced.append(item)
            for item in announced:
                await self._finish_upload(
                    client,
                    item,
                    participant_id=participant_id,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    context_token=context_token,
                    timeout=timeout,
                )
        finally:
            if owns_client:
                await client.close()

    async def _finish_upload(
        self,
        client: WeixinClient,
        item: _PreparedUpload,
        *,
        participant_id: str,
        conversation_id: str | None,
        user_id: str,
        context_token: str,
        timeout: float,
    ) -> None:
        try:
            await self._upload_prepared(
                client,
                user_id,
                context_token,
                item,
                timeout,
            )
        except Exception as error:
            notice = tr(
                "channel.weixin.upload_failed_notice",
                filename=item.filename,
                error=_safe_delivery_error(error),
            )
        else:
            self._activity.record_sent(participant_id)
            notice = tr(
                "channel.weixin.upload_succeeded_notice",
                filename=item.filename,
            )
        await self._send_status_text(
            client,
            user_id,
            context_token,
            participant_id,
            notice,
        )
        await self._push_upload_status(participant_id, conversation_id, notice)

    async def _upload_prepared(
        self,
        client: WeixinClient,
        user_id: str,
        context_token: str,
        item: _PreparedUpload,
        timeout: float,
    ) -> None:
        limit = self._config.max_upload_mb * 1024 * 1024
        rawfilemd5, rawsize = await asyncio.to_thread(_md5_and_size, item.path, limit)
        kind = _outbound_kind(
            item.declared,
            {"filename": item.filename, "media_type": item.media_type},
            item.path,
        )
        ciphertext_size = aes_ecb_padded_size(rawsize)
        key = secrets.token_bytes(16)
        filekey = secrets.token_hex(16)
        upload_url = await client.resolve_upload_url(
            user_id=user_id,
            media_type=_UPLOAD_MEDIA_TYPES[kind],
            filekey=filekey,
            rawsize=rawsize,
            rawfilemd5=rawfilemd5,
            filesize=ciphertext_size,
            aeskey_hex=key.hex(),
            cdn_base_url=DEFAULT_CDN_BASE_URL,
        )
        descriptor, name = tempfile.mkstemp(prefix="coworker-weixin-")
        os.close(descriptor)
        ciphertext_path = Path(name)
        try:
            written = await asyncio.to_thread(
                encrypt_aes_ecb_file,
                item.path,
                ciphertext_path,
                key,
            )
            if written != ciphertext_size:
                raise WeixinDeliveryError(tr("channel.weixin.upload_failed"))
            download_param = await client.upload_ciphertext(
                upload_url,
                ciphertext_path,
                timeout=timeout,
            )
        finally:
            ciphertext_path.unlink(missing_ok=True)
        await client.send_item(
            user_id,
            _outbound_item(
                kind,
                download_param,
                key,
                rawsize,
                ciphertext_size,
                item.filename,
            ),
            context_token,
        )

    async def _send_status_text(
        self,
        client: WeixinClient,
        user_id: str,
        context_token: str,
        participant_id: str,
        notice: str,
    ) -> None:
        try:
            await client.send_text(user_id, notice, context_token)
        except Exception as error:
            logger.warning(
                tr(
                    "channel.weixin.upload_status_delivery_failed",
                    error_type=type(error).__name__,
                )
            )
            return
        self._activity.record_sent(participant_id)

    async def _push_upload_status(
        self,
        participant_id: str,
        conversation_id: str | None,
        notice: str,
    ) -> None:
        if self._inbound_handler is None:
            return
        try:
            await self._inbound_handler(
                IncomingEvent(
                    participant_id=participant_id,
                    content=tr("channel.weixin.upload_status", notice=notice),
                    conversation_id=conversation_id,
                    source="weixin_upload",
                )
            )
        except Exception as error:
            logger.warning(
                tr(
                    "channel.weixin.upload_status_delivery_failed",
                    error_type=type(error).__name__,
                )
            )

    def _reserve_attachment_path(self, filename: str) -> Path:
        while True:
            candidate = self._attachments_dir / f"{secrets.randbelow(1_000_000):06d}_{filename}"
            try:
                handle = candidate.open("xb")
            except FileExistsError:
                continue
            handle.close()
            return candidate

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


def _item_dict(item: dict[str, Any], key: str) -> dict[str, Any]:
    payload = item.get(key)
    return payload if isinstance(payload, dict) else {}


def _media_refs(message: dict[str, Any]) -> list[_MediaRef]:
    """Extract downloadable media from message items.

    Field requirements mirror the official ``@tencent-weixin/openclaw-weixin``
    client: an image needs CDN coordinates (``encrypt_query_param`` or
    ``full_url``) with an optional key (plain CDN bytes otherwise), while file
    and video items additionally require ``media.aes_key``. Voice stays
    transcript-only, matching the official SILK-transcode boundary.
    """
    refs: list[_MediaRef] = []
    for item in message.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == _IMAGE_ITEM_TYPE:
            item_payload = _item_dict(item, "image_item")
            media = _item_dict(item_payload, "media")
            if not (media.get("encrypt_query_param") or media.get("full_url")):
                continue
            refs.append(
                _MediaRef(
                    item_type=item_type,
                    encrypt_query_param=str(media.get("encrypt_query_param") or ""),
                    full_url=str(media.get("full_url") or ""),
                    aeskey_hex=str(item_payload.get("aeskey") or ""),
                    media_aes_key=str(media.get("aes_key") or ""),
                    filename=f"weixin-image-{new_compact_id()}",
                )
            )
        elif item_type in (_FILE_ITEM_TYPE, _VIDEO_ITEM_TYPE):
            key = "file_item" if item_type == _FILE_ITEM_TYPE else "video_item"
            media = _item_dict(_item_dict(item, key), "media")
            if not (media.get("encrypt_query_param") or media.get("full_url")):
                continue
            media_aes_key = str(media.get("aes_key") or "")
            if not media_aes_key:
                continue
            if item_type == _FILE_ITEM_TYPE:
                fallback = f"weixin-file-{new_compact_id()}"
                filename = str(_item_dict(item, "file_item").get("file_name") or fallback)
            else:
                filename = f"weixin-video-{new_compact_id()}"
            refs.append(
                _MediaRef(
                    item_type=item_type,
                    encrypt_query_param=str(media.get("encrypt_query_param") or ""),
                    full_url=str(media.get("full_url") or ""),
                    media_aes_key=media_aes_key,
                    filename=filename,
                )
            )
    return refs


def _safe_delivery_error(error: Exception) -> str:
    """Return an agent-visible reason that does not carry CDN coordinates or keys."""
    if isinstance(error, WeixinDeliveryError):
        return str(error)
    message = str(error)
    if isinstance(error, (FileNotFoundError, ValueError)) and "://" not in message:
        return message
    logger.warning(
        tr("channel.weixin.delivery_failed_type", error_type=type(error).__name__)
    )
    return tr("channel.weixin.delivery_failed")


def _declared_attachment_type(attachment: dict[str, Any]) -> str:
    declared = str(attachment.get("type") or "file")
    if declared not in {"image", "file"}:
        raise ValueError(
            tr("channel.weixin.attachment_type_invalid", type=declared)
        )
    return declared


def _attachment_path(attachment: dict[str, Any]) -> Path:
    raw = str(attachment.get("path") or "").strip()
    if not raw:
        raise ValueError(tr("channel.weixin.attachment_path_required"))
    path = Path(raw)
    if not path.is_file():
        raise FileNotFoundError(tr("channel.weixin.attachment_missing", path=path))
    return path


def _outbound_kind(declared: str, attachment: dict[str, Any], path: Path) -> str:
    filename = str(attachment.get("filename") or path.name)
    media_type = str(attachment.get("media_type") or "") or guess_file_mime(filename)
    if media_type.startswith("video/"):
        return "video"
    if declared == "image" or media_type.startswith("image/"):
        return "image"
    return "file"


def _md5_and_size(path: Path, limit: int) -> tuple[str, int]:
    digest = hashlib.md5()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            size += len(chunk)
            if size > limit:
                raise ValueError(
                    tr(
                        "channel.weixin.attachment_too_large",
                        filename=path.name,
                        size=size,
                        limit=limit,
                    )
                )
            digest.update(chunk)
    return digest.hexdigest(), size


def _outbound_item(
    kind: str,
    download_param: str,
    key: bytes,
    plaintext_size: int,
    ciphertext_size: int,
    filename: str,
) -> dict[str, Any]:
    media = {
        "encrypt_query_param": download_param,
        "aes_key": encode_outbound_aes_key(key),
        "encrypt_type": 1,
    }
    if kind == "image":
        return {
            "type": _IMAGE_ITEM_TYPE,
            "image_item": {"media": media, "mid_size": ciphertext_size},
        }
    if kind == "video":
        return {
            "type": _VIDEO_ITEM_TYPE,
            "video_item": {"media": media, "video_size": ciphertext_size},
        }
    return {
        "type": _FILE_ITEM_TYPE,
        "file_item": {
            "media": media,
            "file_name": filename,
            "len": str(plaintext_size),
        },
    }


def _render_qrcode(content: str) -> bytes:
    image = qrcode.make(content)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
