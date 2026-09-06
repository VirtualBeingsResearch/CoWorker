"""Peer Coworker channel: direct messaging between Coworker instances."""

from __future__ import annotations

import base64
import json
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from loguru import logger
from pydantic import ValidationError

from coworker.channels.activity import ChannelActivityStore
from coworker.channels.base import (
    BaseChannel,
    ChannelCapabilities,
    ConnectionInfo,
)
from coworker.channels.inbound import AttachmentStore, InboundEnvelope
from coworker.core.config import CoworkerConfig, CoworkerPeerConfig
from coworker.core.types import CommunicateRequest, IncomingEvent, ToolResult
from coworker.i18n import tr
from coworker.relay.consumer import RelayConsumerError, relay_request

COWORKER_PREFIX = "coworker:"
CONTROL_PARTICIPANT_ID = f"{COWORKER_PREFIX}control"
RESERVED_PEER_IDS = frozenset({"control"})
_SELF_ID_FILE = "coworker_self_id.txt"
_SELF_ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,31}")


def resolve_coworker_self_id(identity_dir: str | Path, *, configured: str = "") -> str:
    """Return this instance's peer id, generating and persisting one if unset."""

    configured = configured.strip()
    if configured:
        if not _SELF_ID_PATTERN.fullmatch(configured):
            raise ValueError(tr("config.coworker.self_id_invalid", self_id=configured))
        return configured
    path = Path(identity_dir) / _SELF_ID_FILE
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    generated = f"cw_{secrets.token_hex(4)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generated, encoding="utf-8")
    logger.info(f"Generated Coworker peer self_id: {generated}")
    return generated


@dataclass(frozen=True)
class CoworkerAnnounce:
    """Self-disclosure attached to outbound messages so peers can reply."""

    base_url: str
    token: str = ""
    display_name: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"base_url": self.base_url}
        if self.token:
            payload["token"] = self.token
        if self.display_name:
            payload["display_name"] = self.display_name
        return payload


@dataclass
class LearnedPeer:
    base_url: str
    token: str = ""
    display_name: str = ""
    last_seen_at: str = ""


class CoworkerPeerStore:
    """Persisted learned peers announced by inbound ``coworker:`` messages."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._peers: dict[str, LearnedPeer] = self._load()

    def get(self, peer_id: str) -> LearnedPeer | None:
        with self._lock:
            return self._peers.get(peer_id)

    def peer_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._peers)

    def upsert(
        self,
        peer_id: str,
        *,
        base_url: str,
        token: str,
        display_name: str,
    ) -> bool:
        """Record an announce; return True when it conflicts with the stored one."""

        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            existing = self._peers.get(peer_id)
            conflict = existing is not None and (
                existing.base_url != base_url or existing.token != token
            )
            self._peers[peer_id] = LearnedPeer(
                base_url=base_url,
                token=token,
                display_name=display_name or (existing.display_name if existing else ""),
                last_seen_at=now,
            )
            self._save()
        return conflict

    def forget(self, peer_id: str) -> bool:
        """Remove a learned peer; return False when it was not stored."""

        with self._lock:
            if peer_id not in self._peers:
                return False
            del self._peers[peer_id]
            self._save()
            return True

    def _load(self) -> dict[str, LearnedPeer]:
        if self._path is None or not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            logger.warning(tr("channel.coworker.store_load_failed", error=error))
            return {}
        peers: dict[str, LearnedPeer] = {}
        if isinstance(raw, dict):
            for peer_id, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                base_url = str(entry.get("base_url") or "")
                if not base_url:
                    continue
                peers[str(peer_id)] = LearnedPeer(
                    base_url=base_url,
                    token=str(entry.get("token") or ""),
                    display_name=str(entry.get("display_name") or ""),
                    last_seen_at=str(entry.get("last_seen_at") or ""),
                )
        return peers

    def _save(self) -> None:
        if self._path is None:
            return
        payload = {
            peer_id: {
                "base_url": peer.base_url,
                "token": peer.token,
                "display_name": peer.display_name,
                "last_seen_at": peer.last_seen_at,
            }
            for peer_id, peer in self._peers.items()
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as error:
            logger.warning(tr("channel.coworker.store_save_failed", error=error))


class CoworkerRuntime:
    """Own the outbound HTTP client lifecycle for the peer channel."""

    name = "coworker"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(follow_redirects=False)
        return self._client

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


def is_relay_instance_url(base_url: str) -> bool:
    """Whether a peer base_url points at a Relay instance entry."""

    parsed = urlsplit(base_url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    return bool(parsed.scheme in {"http", "https"} and segments and segments[0] == "i")


class CoworkerChannel(BaseChannel):
    """Exchange messages with peer Coworker instances over their HTTP API."""

    name = "coworker"
    participant_prefix = COWORKER_PREFIX
    requires_known_participant = True

    def __init__(
        self,
        *,
        self_id: str,
        peers: dict[str, CoworkerPeerConfig],
        learned: CoworkerPeerStore,
        attachments: AttachmentStore,
        announce: CoworkerAnnounce | None = None,
        max_attachment_bytes: int = 10 * 1024 * 1024,
        runtime: CoworkerRuntime | None = None,
        activity: ChannelActivityStore | None = None,
        timeout_seconds: float = 20.0,
        fallback_base_url: str = "",
        fallback_token: str = "",
        identity_dir: str | Path | None = None,
    ) -> None:
        super().__init__(runtime=runtime or CoworkerRuntime(), activity=activity)
        self._capabilities = ChannelCapabilities(conversation_id=True, attachments=True)
        self._self_id = self_id
        self._peers = peers
        self._learned = learned
        self._attachments = attachments
        self._announce = announce
        self._max_attachment_bytes = max_attachment_bytes
        self._timeout_seconds = timeout_seconds
        self._fallback_base_url = fallback_base_url.rstrip("/")
        self._fallback_token = fallback_token
        self._identity_dir = Path(identity_dir) if identity_dir else None

    @property
    def self_id(self) -> str:
        return self._self_id

    def agent_instructions(self) -> str:
        return tr("prompt.channel.coworker")

    def capabilities_for(self, participant_id: str) -> ChannelCapabilities:
        if participant_id == CONTROL_PARTICIPANT_ID:
            return ChannelCapabilities(extra=True)
        return ChannelCapabilities(conversation_id=True, attachments=True)

    def reconfigure(self, config: CoworkerConfig) -> None:
        """Replace explicit peers and announce settings without restarting."""

        self._peers = dict(config.peers)
        if config.self_id:
            self._self_id = config.self_id
            # 持久化到 identity 目录：管理端之后清空配置时，重启仍沿用最后生效的身份。
            self._persist_self_id()
        self._max_attachment_bytes = config.max_attachment_bytes
        base_url = config.self_base_url or self._fallback_base_url
        if self._announce is not None:
            base_url = base_url or self._announce.base_url
        if base_url:
            self._announce = CoworkerAnnounce(
                base_url=base_url,
                token=config.inbound_token or self._fallback_token,
                display_name=(
                    self._announce.display_name if self._announce is not None else self._self_id
                ),
            )

    def _persist_self_id(self) -> None:
        if self._identity_dir is None:
            return
        try:
            path = self._identity_dir / _SELF_ID_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_file() and path.read_text(encoding="utf-8").strip() == self._self_id:
                return
            path.write_text(self._self_id, encoding="utf-8")
        except OSError as error:
            logger.warning(
                tr("channel.coworker.self_id_persist_failed", error=str(error))
            )

    def list_connections(self) -> list[ConnectionInfo]:
        connections: dict[str, ConnectionInfo] = {
            "control": ConnectionInfo(
                participant_id=CONTROL_PARTICIPANT_ID,
                channel=self.name,
                kind="coworker:control",
                display_name=tr("channel.coworker.control_name"),
                active=True,
            )
        }
        for peer_id, peer in self._peers.items():
            connections[peer_id] = ConnectionInfo(
                participant_id=f"{COWORKER_PREFIX}{peer_id}",
                channel=self.name,
                kind="peer",
                display_name=peer.display_name,
            )
        for peer_id in self._learned.peer_ids():
            if peer_id in connections:
                continue
            learned = self._learned.get(peer_id)
            if learned is None:
                continue
            connections[peer_id] = ConnectionInfo(
                participant_id=f"{COWORKER_PREFIX}{peer_id}",
                channel=self.name,
                kind="peer",
                display_name=learned.display_name,
            )
        result: list[ConnectionInfo] = []
        for connection in connections.values():
            last_sent_at, last_received_at = self.activity_for(connection.participant_id)
            result.append(
                ConnectionInfo(
                    participant_id=connection.participant_id,
                    channel=connection.channel,
                    kind=connection.kind,
                    display_name=connection.display_name,
                    active=False,
                    last_sent_at=last_sent_at,
                    last_received_at=last_received_at,
                )
            )
        return result

    async def receive_raw(self, envelope: InboundEnvelope) -> None:
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}
        peer_id = envelope.participant_id.removeprefix(COWORKER_PREFIX)
        announce = self._parse_announce(payload.get("coworker_peer"))
        if announce is not None and peer_id not in self._peers:
            conflict = self._learned.upsert(
                peer_id,
                base_url=announce.base_url,
                token=announce.token,
                display_name=announce.display_name,
            )
            if conflict:
                logger.warning(
                    tr(
                        "channel.coworker.peer_conflict",
                        participant=envelope.participant_id,
                    )
                )
        content = str(payload.get("content") or "")
        conversation = payload.get("conversation_id")
        conversation_id = conversation if isinstance(conversation, str) else None
        attachments = [
            self._attachments.save(item, keep_inline_data=True)
            for item in payload.get("attachments", [])
            if isinstance(item, dict)
        ]
        self.record_received(envelope.participant_id)
        await self.publish_inbound(
            IncomingEvent(
                participant_id=envelope.participant_id,
                content=content,
                conversation_id=conversation_id,
                source=self.name,
                attachments=attachments,
            )
        )

    async def send(self, request: CommunicateRequest) -> ToolResult:
        if request.participant_id == CONTROL_PARTICIPANT_ID:
            return await self._control(request.extra)
        peer_id = request.participant_id.removeprefix(COWORKER_PREFIX)
        target = self._target_for(peer_id)
        if target is None:
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.communicate.coworker_unknown_target",
                    participant=request.participant_id,
                ),
                is_error=True,
            )
        encoded, error = self._encode_attachments(request.attachments)
        if error is not None:
            return error
        body: dict[str, Any] = {
            "sender_id": f"{COWORKER_PREFIX}{self._self_id}",
            "content": request.message,
        }
        if request.conversation_id:
            body["conversation_id"] = request.conversation_id
        if encoded:
            body["attachments"] = encoded
        if self._announce is not None:
            body["coworker_peer"] = self._announce.to_payload()
        headers = [("content-type", "application/json")]
        if target.token:
            headers.append(("authorization", f"Bearer {target.token}"))
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if is_relay_instance_url(target.base_url):
            return await self._send_via_relay(
                request.participant_id, target, headers, payload
            )
        try:
            response = await self._client().post(
                f"{target.base_url}/messages",
                content=payload,
                headers=dict(headers),
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as error:
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.communicate.coworker_unreachable",
                    participant=request.participant_id,
                    base_url=target.base_url,
                    error=type(error).__name__,
                ),
                is_error=True,
            )
        return self._result_for_status(request.participant_id, response.status_code)

    async def _send_via_relay(
        self,
        participant_id: str,
        target: _PeerTarget,
        headers: list[tuple[str, str]],
        payload: bytes,
    ) -> ToolResult:
        try:
            response = await relay_request(
                base_url=target.base_url,
                token=target.token,
                method="POST",
                target="/messages",
                headers=headers,
                body=payload,
                timeout_seconds=self._timeout_seconds + 15.0,
            )
        except RelayConsumerError as error:
            params = dict(error.params)
            params.setdefault("participant", participant_id)
            return ToolResult(
                tool_call_id="",
                content=tr(error.message_key, **params),
                is_error=True,
            )
        return self._result_for_status(participant_id, response.status)

    def _result_for_status(self, participant_id: str, status: int) -> ToolResult:
        """Map an HTTP status from either transport onto the shared tool results."""

        if status in (401, 403):
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.communicate.coworker_unauthorized",
                    participant=participant_id,
                    status=status,
                ),
                is_error=True,
            )
        if status >= 400:
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.communicate.coworker_failed",
                    participant=participant_id,
                    status=status,
                ),
                is_error=True,
            )
        self._record_sent(participant_id)
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.communicate.coworker_sent",
                participant=participant_id,
            ),
        )

    def _client(self) -> httpx.AsyncClient:
        runtime = self.runtime
        assert isinstance(runtime, CoworkerRuntime)
        return runtime.client()

    def _target_for(self, peer_id: str) -> _PeerTarget | None:
        configured = self._peers.get(peer_id)
        if configured is not None:
            return _PeerTarget(
                base_url=configured.base_url,
                token=configured.token,
                display_name=configured.display_name,
            )
        learned = self._learned.get(peer_id)
        if learned is not None:
            return _PeerTarget(
                base_url=learned.base_url,
                token=learned.token,
                display_name=learned.display_name,
            )
        return None

    def _encode_attachments(
        self,
        attachments: list[dict[str, Any]],
    ) -> tuple[list[dict[str, str]], ToolResult | None]:
        encoded: list[dict[str, str]] = []
        total = 0
        for attachment in attachments:
            path = str(attachment.get("path") or "")
            if not path:
                continue
            source = Path(path)
            try:
                raw = source.read_bytes()
            except OSError as error:
                return [], ToolResult(
                    tool_call_id="",
                    content=tr(
                        "tool_result.communicate.coworker_attachment_read_failed",
                        path=path,
                        error=type(error).__name__,
                    ),
                    is_error=True,
                )
            total += len(raw)
            if total > self._max_attachment_bytes:
                return [], ToolResult(
                    tool_call_id="",
                    content=tr(
                        "tool_result.communicate.coworker_attachment_too_large",
                        filename=str(attachment.get("filename") or source.name),
                        size=len(raw),
                        limit=self._max_attachment_bytes,
                    ),
                    is_error=True,
                )
            encoded.append(
                {
                    "filename": str(attachment.get("filename") or source.name),
                    "media_type": str(attachment.get("media_type") or "application/octet-stream"),
                    "data": base64.b64encode(raw).decode("ascii"),
                }
            )
        return encoded, None

    @staticmethod
    def _parse_announce(raw: Any) -> CoworkerAnnounce | None:
        if not isinstance(raw, dict):
            return None
        base_url = str(raw.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            return None
        return CoworkerAnnounce(
            base_url=base_url,
            token=str(raw.get("token") or ""),
            display_name=str(raw.get("display_name") or ""),
        )

    async def _control(self, extra: dict[str, Any] | None) -> ToolResult:
        payload = extra or {}
        action = str(payload.get("action") or "").strip()
        if action == "connect":
            return self._connect_peer(payload)
        if action == "forget":
            return self._forget_peer(payload)
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.communicate.coworker_control_action",
                action=action,
            ),
            is_error=True,
        )

    def _connect_peer(self, extra: dict[str, Any]) -> ToolResult:
        peer_id = _peer_id_from_extra(extra)
        if not peer_id:
            return _control_error(tr("tool_result.communicate.coworker_control_peer_id"))
        if peer_id in RESERVED_PEER_IDS:
            return _control_error(
                tr("tool_result.communicate.coworker_control_reserved", peer=peer_id)
            )
        if peer_id == self._self_id:
            return _control_error(tr("tool_result.communicate.coworker_control_self"))
        if peer_id in self._peers:
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.communicate.coworker_control_already_configured",
                    participant=f"{COWORKER_PREFIX}{peer_id}",
                ),
            )
        if not _SELF_ID_PATTERN.fullmatch(peer_id):
            return _control_error(tr("config.coworker.peer_id_invalid", peer=peer_id))
        try:
            configured = CoworkerPeerConfig(
                base_url=str(extra.get("base_url") or ""),
                token=str(extra.get("token") or ""),
                display_name=str(extra.get("display_name") or ""),
            )
        except ValidationError as error:
            return _control_error(_connect_validation_message(error, peer_id))
        from coworker.relay.crypto import is_relay_safe_token

        if is_relay_instance_url(configured.base_url) and not is_relay_safe_token(
            configured.token
        ):
            return _control_error(
                tr("config.coworker.peer_relay_token_invalid", peer=peer_id)
            )
        self._learned.upsert(
            peer_id,
            base_url=configured.base_url,
            token=configured.token,
            display_name=configured.display_name,
        )
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.communicate.coworker_control_connected",
                participant=f"{COWORKER_PREFIX}{peer_id}",
                base_url=configured.base_url,
            ),
        )

    def _forget_peer(self, extra: dict[str, Any]) -> ToolResult:
        peer_id = _peer_id_from_extra(extra)
        if not peer_id:
            return _control_error(tr("tool_result.communicate.coworker_control_peer_id"))
        if peer_id in self._peers:
            return _control_error(
                tr(
                    "tool_result.communicate.coworker_control_forget_explicit",
                    participant=f"{COWORKER_PREFIX}{peer_id}",
                )
            )
        if extra.get("confirm") is not True:
            return _control_error(
                tr(
                    "tool_result.communicate.coworker_control_confirm_forget",
                    participant=f"{COWORKER_PREFIX}{peer_id}",
                )
            )
        if not self._learned.forget(peer_id):
            return _control_error(
                tr(
                    "tool_result.communicate.coworker_control_unknown_peer",
                    participant=f"{COWORKER_PREFIX}{peer_id}",
                )
            )
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.communicate.coworker_control_forgotten",
                participant=f"{COWORKER_PREFIX}{peer_id}",
            ),
        )


def _peer_id_from_extra(extra: dict[str, Any]) -> str:
    return str(extra.get("peer_id") or "").strip().removeprefix(COWORKER_PREFIX)


def _control_error(content: str) -> ToolResult:
    return ToolResult(tool_call_id="", content=content, is_error=True)


def _connect_validation_message(error: ValidationError, peer_id: str) -> str:
    """Localize peer-config validation failures for coworker:control responses."""

    for item in error.errors():
        loc = item.get("loc") or ()
        if "base_url" in loc:
            return tr(
                "tool_result.communicate.coworker_control_base_url_invalid",
                peer=peer_id,
            )
        if "display_name" in loc:
            return tr("config.coworker.display_name_too_long")
    return tr("tool_result.communicate.coworker_control_peer_invalid", peer=peer_id)


class CoworkerSettings:
    config_key = "coworker"

    def __init__(self, channel: CoworkerChannel) -> None:
        self._channel = channel

    async def apply(self, config: object) -> None:
        if not isinstance(config, CoworkerConfig):
            raise TypeError(tr("channel.coworker.config_type_invalid"))
        self._channel.reconfigure(config)
        from coworker.api.routes import update_coworker_peer_auth

        update_coworker_peer_auth(
            inbound_token=config.inbound_token,
            self_id=self._channel.self_id,
        )


@dataclass(frozen=True)
class CoworkerModule:
    name = "coworker"

    channel: CoworkerChannel
    runtime: CoworkerRuntime
    settings: CoworkerSettings
    management: None = None


def create_coworker_module(
    config: CoworkerConfig,
    *,
    self_id: str,
    learned: CoworkerPeerStore,
    attachments: AttachmentStore,
    announce: CoworkerAnnounce | None,
    activity: ChannelActivityStore,
    fallback_base_url: str = "",
    fallback_token: str = "",
    identity_dir: str | Path | None = None,
) -> CoworkerModule:
    runtime = CoworkerRuntime()
    channel = CoworkerChannel(
        self_id=self_id,
        peers=config.peers,
        learned=learned,
        attachments=attachments,
        announce=announce,
        max_attachment_bytes=config.max_attachment_bytes,
        runtime=runtime,
        activity=activity,
        fallback_base_url=fallback_base_url,
        fallback_token=fallback_token,
        identity_dir=identity_dir,
    )
    return CoworkerModule(
        channel=channel,
        runtime=runtime,
        settings=CoworkerSettings(channel),
    )


@dataclass(frozen=True)
class _PeerTarget:
    base_url: str
    token: str
    display_name: str
