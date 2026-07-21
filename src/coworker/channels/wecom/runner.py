from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from coworker.channels.wecom import adapter
from coworker.core.types import ToolResult
from coworker.i18n import tr


class _LoguruLogger:
    """把 SDK 的 debug/info/warn/error 调用转发到项目的 loguru。

    SDK 期望的协议：四个方法，签名 (self, message: str, *args: Any) -> None。
    DefaultLogger 默认 print 到 stdout、UTC 时间、AiBotSDK 前缀，与项目日志格式不一致。
    """

    def __init__(self, prefix: str = "wecom") -> None:
        self._prefix = prefix

    def _fmt(self, message: str, args: tuple[Any, ...]) -> str:
        if args:
            extra = " ".join(str(a) for a in args)
            return f"[{self._prefix}] {message} {extra}".rstrip()
        return f"[{self._prefix}] {message}"

    def debug(self, message: str, *args: Any) -> None:
        logger.opt(depth=1).debug(self._fmt(message, args))

    def info(self, message: str, *args: Any) -> None:
        logger.opt(depth=1).info(self._fmt(message, args))

    def warn(self, message: str, *args: Any) -> None:
        logger.opt(depth=1).warning(self._fmt(message, args))

    def error(self, message: str, *args: Any) -> None:
        logger.opt(depth=1).error(self._fmt(message, args))


if TYPE_CHECKING:
    from coworker.agent.inbox_watcher import InboxWatcher
    from coworker.core.config import WeComConfig
    from coworker.core.types import CommunicateRequest

_FRAME_TTL = 600.0  # 10 minutes
_MEDIA_LIMITS = {
    "image": 10 * 1024 * 1024,
    "file": 20 * 1024 * 1024,
    "voice": 2 * 1024 * 1024,
    "video": 10 * 1024 * 1024,
}
_MARKDOWN_MAX_BYTES = 20480


def _normalize_chat_type(chat_type: Any) -> str | None:
    if chat_type in ("single", "group"):
        return chat_type
    if chat_type == 1:
        return "single"
    if chat_type == 2:
        return "group"
    return None


def _split_markdown(text: str, max_bytes: int = _MARKDOWN_MAX_BYTES) -> list[str]:
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    chunks: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        candidate = f"{buf}\n\n{para}" if buf else para
        if len(candidate.encode("utf-8")) <= max_bytes:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
        if len(para.encode("utf-8")) > max_bytes:
            # Hard split a single oversize paragraph by characters until each piece fits.
            piece = ""
            for ch in para:
                if len((piece + ch).encode("utf-8")) > max_bytes:
                    chunks.append(piece)
                    piece = ch
                else:
                    piece += ch
            buf = piece
        else:
            buf = para
    if buf:
        chunks.append(buf)
    return chunks


class WeComRunner:
    def __init__(
        self,
        cfg: WeComConfig,
        inbox: InboxWatcher,
        attachments_dir: Path,
        contacts_path: Path | None = None,
    ) -> None:
        self._cfg = cfg
        self._inbox = inbox
        self._attachments_dir = attachments_dir
        self._contacts_path = contacts_path
        self._client: Any = None  # WSClient, lazy-imported
        self._frame_cache: dict[str, tuple[dict[str, Any], float]] = {}
        self._media_cache: dict[tuple[str, float], str] = {}
        # Persistent chat_id → chat_type ("single"/"group") mapping.
        self._contacts: dict[str, str] = self._load_contacts()
        self._stop = asyncio.Event()
        self._kicked = False

    def _load_contacts(self) -> dict[str, str]:
        if self._contacts_path and self._contacts_path.exists():
            try:
                raw = json.loads(self._contacts_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    return {}
                contacts: dict[str, str] = {}
                for chat_id, chat_type in raw.items():
                    normalized = _normalize_chat_type(chat_type)
                    if normalized is not None:
                        contacts[str(chat_id)] = normalized
                return contacts
            except Exception as e:
                logger.warning(f"WeCom contacts load failed: {e}")
        return {}

    def _save_contacts(self) -> None:
        if not self._contacts_path:
            return
        try:
            self._contacts_path.parent.mkdir(parents=True, exist_ok=True)
            self._contacts_path.write_text(
                json.dumps(self._contacts, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"WeCom contacts save failed: {e}")

    async def start(self) -> None:
        from wecom_aibot_sdk import WSClient

        self._client = WSClient(
            bot_id=self._cfg.bot_id,
            secret=self._cfg.secret,
            ws_url=self._cfg.ws_url or "",
            logger=_LoguruLogger(),
        )
        self._register_handlers()
        try:
            await self._client.connect()
        except Exception as e:
            logger.error(f"WeCom connect failed: {e}")
            return

        # Periodically drop expired frame cache entries; exit when stop is signaled.
        try:
            while not self._stop.is_set() and not self._kicked:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=60.0)
                except TimeoutError:
                    pass
                self._sweep_frames()
        finally:
            await self.stop()

    async def stop(self) -> None:
        self._stop.set()
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as e:
                logger.debug(f"WeCom disconnect error: {e}")

    # ── handler registration ─────────────────────────────────────────────

    def _register_handlers(self) -> None:
        c = self._client
        c.on("authenticated", lambda: logger.info(f"WeCom authenticated bot={self._cfg.bot_id}"))
        c.on("disconnected", lambda reason=None: logger.warning(f"WeCom disconnected: {reason}"))
        c.on("event.disconnected_event", self._on_kicked)
        for evt in ("message.text", "message.voice"):
            c.on(evt, self._on_text_like)
        for evt in ("message.image", "message.file", "message.mixed", "message.video"):
            c.on(evt, self._on_with_attachments)
        c.on("message.stream", self._on_stream_notify)

    async def _on_text_like(self, frame: dict[str, Any]) -> None:
        try:
            event = adapter.frame_to_event(frame, attachments=[])
            self._cache_frame(adapter.participant_id_for(frame), frame)
            await self._inbox.push(event)
        except Exception as e:
            logger.error(f"WeCom text handler error: {e}")

    async def _on_with_attachments(self, frame: dict[str, Any]) -> None:
        try:
            atts = await adapter.collect_attachments(self._client, frame, self._attachments_dir)
            event = adapter.frame_to_event(frame, attachments=atts)
            self._cache_frame(adapter.participant_id_for(frame), frame)
            await self._inbox.push(event)
        except Exception as e:
            logger.error(f"WeCom attachment handler error: {e}")

    async def _on_stream_notify(self, frame: dict[str, Any]) -> None:
        stream_id = frame.get("body", {}).get("stream", {}).get("id", "?")
        logger.debug(f"WeCom stream notify id={stream_id}")

    async def _on_kicked(self, frame: dict[str, Any]) -> None:
        logger.warning("WeCom kicked by a newer connection; will not auto-reconnect")
        self._kicked = True
        self._stop.set()

    # ── frame cache ──────────────────────────────────────────────────────

    def _cache_frame(self, participant_id: str, frame: dict[str, Any]) -> None:
        # Cache keyed by participant_id so send() can look up by chat_id later.
        chat_type, chat_id = adapter.parse_participant(participant_id)
        self._frame_cache[chat_id] = (frame, time.monotonic() + _FRAME_TTL)
        if self._contacts.get(chat_id) != chat_type:
            self._contacts[chat_id] = chat_type
            self._save_contacts()

    def _take_fresh_frame(self, chat_id: str) -> dict[str, Any] | None:
        item = self._frame_cache.pop(chat_id, None)
        if item is None:
            return None
        frame, expires = item
        if time.monotonic() >= expires:
            return None
        return frame

    def _sweep_frames(self) -> None:
        now = time.monotonic()
        expired = [k for k, (_, exp) in self._frame_cache.items() if exp <= now]
        for k in expired:
            self._frame_cache.pop(k, None)

    # ── outbound ─────────────────────────────────────────────────────────

    async def send(
        self,
        participant_id: str,
        message: str,
        attachments: list[dict[str, Any]],
    ) -> None:
        if self._client is None:
            raise RuntimeError("WeCom client not started")
        chat_type, chat_id = adapter.parse_participant(participant_id)
        frame = self._take_fresh_frame(chat_id)

        if message:
            from wecom_aibot_sdk import generate_req_id

            for chunk in _split_markdown(message):
                if frame is not None:
                    await self._client.reply_stream(
                        frame, generate_req_id("stream"), chunk, finish=True
                    )
                    frame = None
                else:
                    await self._client.send_message(
                        chat_id,
                        {"msgtype": "markdown", "markdown": {"content": chunk}},
                    )

        for att in attachments:
            self._validate_attachment(att)
            media_id = await self._ensure_media(att)
            media_type = att["type"]
            if frame is not None:
                await self._client.reply_media(frame, media_type, media_id)
                frame = None
            else:
                await self._client.send_media_message(chat_id, media_type, media_id)

    def _validate_attachment(self, att: dict[str, Any]) -> None:
        media_type = att.get("type")
        if media_type not in _MEDIA_LIMITS:
            raise ValueError(f"unsupported attachment type: {media_type!r}")
        path = Path(att["path"])
        if not path.is_file():
            raise FileNotFoundError(f"attachment not found: {path}")
        size = path.stat().st_size
        if size < 5:
            raise ValueError(f"attachment too small (<5 bytes): {path}")
        limit = _MEDIA_LIMITS[media_type]
        if size > limit:
            raise ValueError(
                f"attachment {path.name} ({size} bytes) exceeds {media_type} limit {limit}"
            )

    async def _ensure_media(self, att: dict[str, Any]) -> str:
        path = Path(att["path"])
        key = (str(path.resolve()), path.stat().st_mtime)
        if key in self._media_cache:
            return self._media_cache[key]
        result = await self._client.upload_media(
            path.read_bytes(),
            type=att["type"],
            filename=path.name,
        )
        media_id = result.media_id if hasattr(result, "media_id") else result["media_id"]
        self._media_cache[key] = media_id
        return media_id

    # ── adapter for CommunicateTool ──────────────────────────────────────

    def checker(self, participant_id: str) -> str | None:
        """若 participant_id 是已知的 WeCom chat_id，返回带前缀的规范化 ID；否则返回 None。"""
        chat_type = _normalize_chat_type(self._contacts.get(participant_id))
        if chat_type is None:
            return None
        return f"wecom:{chat_type}:{participant_id}"

    async def sender(
        self,
        request: CommunicateRequest,
    ) -> ToolResult:
        participant_id = request.participant_id
        try:
            if request.conversation_id:
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.communicate.wecom_conversation_unsupported"),
                    is_error=True,
                )
            if request.extra:
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.communicate.wecom_extra_unsupported"),
                    is_error=True,
                )
            await self.send(participant_id, request.message, request.attachments)
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.wecom_sent", participant=participant_id),
            )
        except Exception as e:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.communicate.wecom_failed", error=e),
                is_error=True,
            )
