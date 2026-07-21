from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from coworker.core.ids import new_compact_id
from coworker.core.types import AttachmentData, IncomingEvent

if TYPE_CHECKING:
    from wecom_aibot_sdk import WSClient

# Map WeCom media URL extension / content-type hint to a media_type usable by the project.
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_INLINE_BASE64_LIMIT = 10 * 1024 * 1024  # >10MB → keep on disk only

_QUOTE_CONTENT_MAX_LEN = 100

_MEDIA_TYPES_BY_EXT: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".json": "application/json",
    ".xml": "text/xml",
    ".html": "text/html",
}


def participant_id_for(frame: dict[str, Any]) -> str:
    body = frame["body"]
    chattype = body.get("chattype", "single")
    if chattype == "group":
        chatid = body.get("chatid") or body["from"]["userid"]
        return f"wecom:group:{chatid}"
    return f"wecom:single:{body['from']['userid']}"


def parse_participant(participant_id: str) -> tuple[str, str]:
    """wecom:single:<userid>  → ("single", userid)
    wecom:group:<chatid>      → ("group", chatid)
    """
    parts = participant_id.split(":", 2)
    if len(parts) != 3 or parts[0] != "wecom":
        raise ValueError(f"not a wecom participant_id: {participant_id}")
    chat_type = parts[1]
    if chat_type not in {"single", "group"}:
        raise ValueError(f"invalid wecom chat type: {participant_id}")
    return chat_type, parts[2]


def _sender_prefix(frame: dict[str, Any]) -> str:
    body = frame["body"]
    if body.get("chattype", "single") != "group":
        return ""
    return f"[发送者 userid={body['from']['userid']}]\n"


def _guess_media_type(filename: str | None, fallback: str) -> str:
    if not filename:
        return fallback
    suffix = Path(filename).suffix.lower()
    return _MEDIA_TYPES_BY_EXT.get(suffix, fallback)


async def _save_buffer(
    buffer: bytes,
    filename: str,
    media_type: str,
    attachments_dir: Path,
) -> AttachmentData:
    attachments_dir.mkdir(parents=True, exist_ok=True)
    dest = attachments_dir / f"{new_compact_id()}_{filename}"
    dest.write_bytes(buffer)
    inline = len(buffer) <= _INLINE_BASE64_LIMIT and media_type.startswith("image/")
    return AttachmentData(
        filename=filename,
        media_type=media_type,
        saved_path=str(dest),
        data=base64.b64encode(buffer).decode("ascii") if inline else None,
    )


async def _download_one(
    client: WSClient,
    url: str,
    aeskey: str | None,
    fallback_filename: str,
    fallback_media_type: str,
    attachments_dir: Path,
) -> AttachmentData | None:
    try:
        result = await client.download_file(url, aeskey)
    except Exception as e:
        logger.error(f"WeCom download failed url={url[:60]}... err={e}")
        return None
    buffer = result.get("buffer", b"")
    filename = result.get("filename") or fallback_filename
    media_type = _guess_media_type(filename, fallback_media_type)
    return await _save_buffer(buffer, filename, media_type, attachments_dir)


async def collect_attachments(
    client: WSClient,
    frame: dict[str, Any],
    attachments_dir: Path,
) -> list[AttachmentData]:
    body = frame["body"]
    msgtype = body.get("msgtype")
    msgid = body.get("msgid", "wecom")
    out: list[AttachmentData] = []

    if msgtype == "image":
        img = body.get("image", {})
        att = await _download_one(
            client,
            img.get("url", ""),
            img.get("aeskey"),
            f"{msgid}.jpg",
            "image/jpeg",
            attachments_dir,
        )
        if att:
            out.append(att)
    elif msgtype == "file":
        fl = body.get("file", {})
        att = await _download_one(
            client,
            fl.get("url", ""),
            fl.get("aeskey"),
            f"{msgid}.bin",
            "application/octet-stream",
            attachments_dir,
        )
        if att:
            out.append(att)
    elif msgtype == "video":
        vid = body.get("video", {})
        att = await _download_one(
            client,
            vid.get("url", ""),
            vid.get("aeskey"),
            f"{msgid}.mp4",
            "video/mp4",
            attachments_dir,
        )
        if att:
            out.append(att)
    elif msgtype == "mixed":
        for idx, item in enumerate(body.get("mixed", {}).get("msg_item", [])):
            if item.get("msgtype") == "image":
                img = item.get("image", {})
                att = await _download_one(
                    client,
                    img.get("url", ""),
                    img.get("aeskey"),
                    f"{msgid}_{idx}.jpg",
                    "image/jpeg",
                    attachments_dir,
                )
                if att:
                    out.append(att)
    return out


def _truncate(text: str) -> str:
    return text if len(text) <= _QUOTE_CONTENT_MAX_LEN else text[:_QUOTE_CONTENT_MAX_LEN] + "..."


_MEDIA_TYPE_ZH: dict[str, str] = {
    "image": "图片",
    "file": "文件",
    "video": "视频",
}


def _quote_prefix(body: dict[str, Any], bot_id: str = "") -> str:
    quote = body.get("msgquote") or body.get("quote") or {}
    if not quote:
        return ""
    qtype = quote.get("msgtype", "")
    from_user = quote.get("from_userid", "")
    if from_user and from_user == bot_id:
        prefix = "引用自己的消息"
        possessive = "引用自己的"
    elif from_user:
        prefix = f"引用 {from_user}"
        possessive = f"引用 {from_user} 的"
    else:
        prefix = "引用"
        possessive = "引用的"
    if qtype in ("text", "voice"):
        content = quote.get(qtype, {}).get("content", "")
        if not content:
            return ""
        return f'[{prefix}: "{_truncate(content)}"]\n'
    elif qtype == "mixed":
        items = quote.get("mixed", {}).get("msg_item", [])
        parts = [
            item.get("text", {}).get("content", "")
            for item in items
            if item.get("msgtype") == "text"
        ]
        content = "\n".join(p for p in parts if p)
        image_count = sum(1 for item in items if item.get("msgtype") == "image")
        image_hint = f"（含 {image_count} 张图片）" if image_count else ""
        if not content:
            return ""
        return f'[{prefix}: "{_truncate(content)}"{image_hint}]\n'
    elif qtype in _MEDIA_TYPE_ZH:
        payload = quote.get(qtype, {})
        url = payload.get("url", "")
        name = payload.get("name") or payload.get("filename") or ""
        zh_label = _MEDIA_TYPE_ZH[qtype]
        label = f'{zh_label} "{name}"' if (qtype == "file" and name) else zh_label
        suffix = f": {url}" if url else ""
        return f"[{possessive}{label}{suffix}]\n"
    elif qtype:
        return f"[{possessive} {qtype} 消息（无法预览）]\n"
    return ""


def _content_for(frame: dict[str, Any]) -> str:
    body = frame["body"]
    bot_id = body.get("aibotid", "")
    quote = _quote_prefix(body, bot_id)
    msgtype = body.get("msgtype")
    if msgtype == "text":
        raw = body.get("text", {}).get("content", "")
    elif msgtype == "voice":
        raw = body.get("voice", {}).get("content", "")
    elif msgtype == "mixed":
        parts: list[str] = []
        for item in body.get("mixed", {}).get("msg_item", []):
            if item.get("msgtype") == "text":
                parts.append(item.get("text", {}).get("content", ""))
        raw = "\n".join(p for p in parts if p)
    else:
        raw = ""
    return quote + raw if quote else raw


def frame_to_event(
    frame: dict[str, Any],
    attachments: list[AttachmentData],
) -> IncomingEvent:
    pid = participant_id_for(frame)
    raw = _content_for(frame)
    content = _sender_prefix(frame) + raw
    return IncomingEvent(
        participant_id=pid,
        content=content,
        timestamp=datetime.now(),
        source="wecom",
        attachments=attachments,
    )
