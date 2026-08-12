from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coworker.channels.telegram.state import TelegramContact
from coworker.i18n import tr


@dataclass(frozen=True)
class TelegramMedia:
    file_id: str
    filename: str
    media_type: str
    label_key: str


def message_for_update(update: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("message", "channel_post"):
        message = update.get(key)
        if isinstance(message, dict):
            return message
    return None


def contact_for(message: dict[str, Any]) -> TelegramContact:
    chat = message.get("chat")
    if not isinstance(chat, dict) or not isinstance(chat.get("id"), int):
        raise ValueError(tr("channel.telegram.chat_invalid"))
    kind = _chat_kind(str(chat.get("type") or ""))
    username = str(chat.get("username") or "")
    if kind == "private":
        display_name = " ".join(
            part
            for part in (
                str(chat.get("first_name") or "").strip(),
                str(chat.get("last_name") or "").strip(),
            )
            if part
        )
    else:
        display_name = str(chat.get("title") or "").strip()
    if not display_name and username:
        display_name = f"@{username}"
    return TelegramContact(
        chat_id=chat["id"],
        kind=kind,
        display_name=display_name,
        username=username,
    )


def participant_id_for(instance_id: str, message: dict[str, Any]) -> str:
    return contact_for(message).participant_id(instance_id)


def conversation_id_for(message: dict[str, Any]) -> str | None:
    thread_id = message.get("message_thread_id")
    return str(thread_id) if isinstance(thread_id, int) else None


def message_content(message: dict[str, Any], media: TelegramMedia | None) -> str:
    text = str(message.get("text") or message.get("caption") or "")
    if not text and media is not None:
        text = tr(media.label_key)
    if not text:
        text = tr("channel.telegram.unsupported")
    prefix = _sender_prefix(message)
    return f"{prefix}{text}" if prefix else text


def media_for(message: dict[str, Any]) -> TelegramMedia | None:
    photos = message.get("photo")
    if isinstance(photos, list):
        photo = next((item for item in reversed(photos) if isinstance(item, dict)), None)
        if photo is not None and photo.get("file_id"):
            unique = str(photo.get("file_unique_id") or photo["file_id"])
            return TelegramMedia(
                file_id=str(photo["file_id"]),
                filename=f"telegram-photo-{unique}.jpg",
                media_type="image/jpeg",
                label_key="channel.telegram.image",
            )
    candidates = (
        ("document", "application/octet-stream", "channel.telegram.file", ".bin"),
        ("video", "video/mp4", "channel.telegram.video", ".mp4"),
        ("audio", "audio/mpeg", "channel.telegram.audio", ".mp3"),
        ("voice", "audio/ogg", "channel.telegram.voice", ".ogg"),
        ("animation", "video/mp4", "channel.telegram.animation", ".mp4"),
    )
    for key, fallback_type, label_key, suffix in candidates:
        payload = message.get(key)
        if not isinstance(payload, dict) or not payload.get("file_id"):
            continue
        filename = str(payload.get("file_name") or "")
        if not filename:
            unique = str(payload.get("file_unique_id") or payload["file_id"])
            filename = f"telegram-{key}-{unique}{suffix}"
        return TelegramMedia(
            file_id=str(payload["file_id"]),
            filename=Path(filename).name,
            media_type=str(payload.get("mime_type") or fallback_type),
            label_key=label_key,
        )
    return None


def parse_participant(participant_id: str) -> tuple[str, int]:
    parts = participant_id.split(":", maxsplit=2)
    if len(parts) != 3 or parts[0] != "tg" or not parts[1]:
        raise ValueError(
            tr("channel.telegram.participant_invalid", participant=participant_id)
        )
    try:
        chat_id = int(parts[2])
    except ValueError as error:
        raise ValueError(
            tr("channel.telegram.participant_invalid", participant=participant_id)
        ) from error
    return parts[1], chat_id


def _chat_kind(chat_type: str) -> str:
    if chat_type == "private":
        return "private"
    if chat_type in {"group", "supergroup"}:
        return "group"
    if chat_type == "channel":
        return "channel"
    raise ValueError(tr("channel.telegram.chat_type_invalid", type=chat_type))


def _sender_prefix(message: dict[str, Any]) -> str:
    chat = message.get("chat") or {}
    if not isinstance(chat, dict) or chat.get("type") not in {"group", "supergroup"}:
        return ""
    sender = message.get("from")
    if not isinstance(sender, dict):
        sender = message.get("sender_chat")
    if not isinstance(sender, dict):
        return ""
    sender_id = sender.get("id")
    username = str(sender.get("username") or "")
    name = str(sender.get("title") or "").strip()
    if not name:
        name = " ".join(
            part
            for part in (
                str(sender.get("first_name") or "").strip(),
                str(sender.get("last_name") or "").strip(),
            )
            if part
        )
    return tr(
        "channel.telegram.group_sender",
        sender=sender_id if isinstance(sender_id, int) else "?",
        username=f"@{username}" if username else "-",
        name=name or "-",
    )
