from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from coworker.core.ids import new_compact_id
from coworker.core.types import AttachmentData, IncomingEvent
from coworker.i18n import tr

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


@dataclass(slots=True)
class _AttachmentCollection:
    attachments: list[AttachmentData]
    failure_notices: list[str]


def participant_id_for(frame: dict[str, Any]) -> str:
    body = frame["body"]
    chattype = body.get("chattype", "single")
    if chattype == "group":
        chatid = body.get("chatid") or body["from"]["userid"]
        return f"wecom:group:{chatid}"
    return f"wecom:single:{body['from']['userid']}"


def conversation_id_for(frame: dict[str, Any]) -> str | None:
    if frame.get("body", {}).get("chattype", "single") != "group":
        return None
    request_id = frame.get("headers", {}).get("req_id")
    if isinstance(request_id, str) and request_id.strip():
        return request_id.strip()
    message_id = frame.get("body", {}).get("msgid")
    if isinstance(message_id, str) and message_id.strip():
        return message_id.strip()
    return None


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
    return tr("channel.wecom.group_sender", userid=body["from"]["userid"])


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
        buffer = result.get("buffer", b"")
        filename = result.get("filename") or fallback_filename
        media_type = _guess_media_type(filename, fallback_media_type)
        return await _save_buffer(buffer, filename, media_type, attachments_dir)
    except Exception as e:
        logger.error(f"WeCom download failed url={url[:60]}... err={e}")
        return None


def _download_failure_notice(
    msgtype: str,
    media: dict[str, Any],
    *,
    quoted: bool,
    index: int | None = None,
) -> str:
    if msgtype == "file" and (name := media.get("name") or media.get("filename")):
        label = tr("channel.wecom.named_file", name=name)
    elif msgtype == "image" and index is not None:
        label = tr("channel.wecom.numbered_image", index=index)
    else:
        label = tr(_MEDIA_TYPE_KEYS[msgtype])
    key = (
        "channel.wecom.quote_attachment_download_failed"
        if quoted
        else "channel.wecom.attachment_download_failed"
    )
    return tr(key, attachment=label)


async def _collect_payload_attachments(
    client: WSClient,
    payload: dict[str, Any],
    msgid: str,
    attachments_dir: Path,
    *,
    quoted: bool,
) -> _AttachmentCollection:
    msgtype = payload.get("msgtype")
    out: list[AttachmentData] = []
    failure_notices: list[str] = []

    media_defaults = {
        "image": ("jpg", "image/jpeg"),
        "file": ("bin", "application/octet-stream"),
        "video": ("mp4", "video/mp4"),
    }
    if msgtype in media_defaults:
        media = payload.get(msgtype, {})
        url = media.get("url", "")
        if not url:
            failure_notices.append(
                _download_failure_notice(msgtype, media, quoted=quoted)
            )
            return _AttachmentCollection(out, failure_notices)
        extension, fallback_media_type = media_defaults[msgtype]
        fallback_filename = media.get("name") or media.get("filename") or f"{msgid}.{extension}"
        att = await _download_one(
            client,
            url,
            media.get("aeskey"),
            fallback_filename,
            fallback_media_type,
            attachments_dir,
        )
        if att:
            out.append(att)
        else:
            failure_notices.append(
                _download_failure_notice(msgtype, media, quoted=quoted)
            )
    elif msgtype == "mixed":
        image_index = 0
        for idx, item in enumerate(payload.get("mixed", {}).get("msg_item", [])):
            if item.get("msgtype") != "image":
                continue
            image_index += 1
            image = item.get("image", {})
            url = image.get("url", "")
            if not url:
                failure_notices.append(
                    _download_failure_notice(
                        "image",
                        image,
                        quoted=quoted,
                        index=image_index,
                    )
                )
                continue
            att = await _download_one(
                client,
                url,
                image.get("aeskey"),
                f"{msgid}_{idx}.jpg",
                "image/jpeg",
                attachments_dir,
            )
            if att:
                out.append(att)
            else:
                failure_notices.append(
                    _download_failure_notice(
                        "image",
                        image,
                        quoted=quoted,
                        index=image_index,
                    )
                )
    return _AttachmentCollection(out, failure_notices)


async def collect_attachments(
    client: WSClient,
    frame: dict[str, Any],
    attachments_dir: Path,
) -> _AttachmentCollection:
    body = frame["body"]
    msgid = body.get("msgid", "wecom")
    collected = await _collect_payload_attachments(
        client,
        body,
        msgid,
        attachments_dir,
        quoted=False,
    )

    quote = body.get("msgquote") or body.get("quote")
    if isinstance(quote, dict):
        quoted = await _collect_payload_attachments(
            client,
            quote,
            f"{msgid}_quote",
            attachments_dir,
            quoted=True,
        )
        collected.attachments.extend(quoted.attachments)
        collected.failure_notices.extend(quoted.failure_notices)
    return collected


def _truncate(text: str) -> str:
    return text if len(text) <= _QUOTE_CONTENT_MAX_LEN else text[:_QUOTE_CONTENT_MAX_LEN] + "..."


_MEDIA_TYPE_KEYS: dict[str, str] = {
    "image": "channel.wecom.image",
    "file": "channel.wecom.file",
    "video": "channel.wecom.video",
}


def _quote_prefix(body: dict[str, Any], bot_id: str = "") -> str:
    quote = body.get("msgquote") or body.get("quote") or {}
    if not quote:
        return ""
    qtype = quote.get("msgtype", "")
    from_user = quote.get("from_userid", "")
    if from_user and from_user == bot_id:
        prefix = tr("channel.wecom.quote_self")
        possessive = tr("channel.wecom.quote_self_possessive")
    elif from_user:
        prefix = tr("channel.wecom.quote_user", user=from_user)
        possessive = tr("channel.wecom.quote_user_possessive", user=from_user)
    else:
        prefix = tr("channel.wecom.quote")
        possessive = tr("channel.wecom.quote_possessive")
    if qtype in ("text", "voice"):
        content = quote.get(qtype, {}).get("content", "")
        if not content:
            return ""
        return tr("channel.wecom.quote_text", prefix=prefix, content=_truncate(content))
    elif qtype == "mixed":
        items = quote.get("mixed", {}).get("msg_item", [])
        parts = [
            item.get("text", {}).get("content", "")
            for item in items
            if item.get("msgtype") == "text"
        ]
        content = "\n".join(p for p in parts if p)
        image_count = sum(1 for item in items if item.get("msgtype") == "image")
        image_hint = tr("channel.wecom.image_count", count=image_count) if image_count else ""
        if not content:
            return ""
        return tr(
            "channel.wecom.quote_mixed",
            prefix=prefix,
            content=_truncate(content),
            image_hint=image_hint,
        )
    elif qtype in _MEDIA_TYPE_KEYS:
        payload = quote.get(qtype, {})
        name = payload.get("name") or payload.get("filename") or ""
        localized_label = tr(_MEDIA_TYPE_KEYS[qtype])
        label = (
            tr("channel.wecom.named_file", name=name)
            if qtype == "file" and name
            else localized_label
        )
        return tr(
            "channel.wecom.quote_media",
            possessive=possessive,
            label=label,
            suffix="",
        )
    elif qtype:
        return tr("channel.wecom.quote_unavailable", possessive=possessive, type=qtype)
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
    attachment_failure_notices: list[str] | None = None,
) -> IncomingEvent:
    pid = participant_id_for(frame)
    raw = _content_for(frame)
    content = _sender_prefix(frame) + raw
    if attachment_failure_notices:
        content = "\n".join([content, *attachment_failure_notices]).lstrip("\n")
    return IncomingEvent(
        participant_id=pid,
        content=content,
        conversation_id=conversation_id_for(frame),
        timestamp=datetime.now(),
        source="wecom",
        attachments=attachments,
    )
