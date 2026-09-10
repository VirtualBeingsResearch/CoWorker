from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coworker.channels.telegram.state import TelegramContact
from coworker.i18n import tr

_REFERENCE_PREVIEW_LIMIT = 1000


@dataclass(frozen=True)
class TelegramMedia:
    file_id: str
    filename: str
    media_type: str
    label_key: str


class TelegramUpdateFormatError(ValueError):
    """An inbound update cannot become a valid Coworker event."""


def message_for_update(update: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("message", "channel_post"):
        message = update.get(key)
        if isinstance(message, dict):
            return message
    return None


def contact_for(message: dict[str, Any]) -> TelegramContact:
    chat = message.get("chat")
    if not isinstance(chat, dict) or not isinstance(chat.get("id"), int):
        raise TelegramUpdateFormatError(tr("channel.telegram.chat_invalid"))
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
    text = _message_body(message, media)
    chat = message.get("chat")
    chat_type = str(chat.get("type") or "") if isinstance(chat, dict) else ""
    kind = _chat_kind(chat_type)
    header = tr(
        "channel.telegram.chat_header",
        type=tr(f"channel.telegram.chat_{kind}"),
    )
    return (
        f"{header}{_sender_prefix(message)}{_forward_prefix(message)}{_reply_prefix(message)}{text}"
    )


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
        # Telegram also exposes animations through the document compatibility field.
        # Check the more specific form before the general document fallback.
        ("animation", "video/mp4", "channel.telegram.animation", ".mp4"),
        ("video", "video/mp4", "channel.telegram.video", ".mp4"),
        ("video_note", "video/mp4", "channel.telegram.video_note", ".mp4"),
        ("audio", "audio/mpeg", "channel.telegram.audio", ".mp3"),
        ("voice", "audio/ogg", "channel.telegram.voice", ".ogg"),
        ("document", "application/octet-stream", "channel.telegram.file", ".bin"),
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
        raise ValueError(tr("channel.telegram.participant_invalid", participant=participant_id))
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
    raise TelegramUpdateFormatError(tr("channel.telegram.chat_type_invalid", type=chat_type))


def _sender_prefix(message: dict[str, Any]) -> str:
    chat = message.get("chat") or {}
    if not isinstance(chat, dict) or chat.get("type") not in {"group", "supergroup"}:
        return ""
    sender = message.get("sender_chat")
    if not isinstance(sender, dict):
        sender = message.get("from")
    if not isinstance(sender, dict):
        return ""
    return tr(
        "channel.telegram.group_sender",
        **_sender_fields(sender),
    )


def _message_body(
    message: dict[str, Any],
    media: TelegramMedia | None = None,
) -> str:
    text = str(message.get("text") or message.get("caption") or "")
    detected_media = media or media_for(message)
    if detected_media is not None:
        media_summary = _media_summary(detected_media)
        return f"{media_summary}\n{text}" if text else media_summary
    if text:
        return text
    structured = _structured_summary(message)
    return structured or tr("channel.telegram.unsupported")


def _message_preview(message: dict[str, Any]) -> str:
    text = str(message.get("text") or message.get("caption") or "").strip()
    media = media_for(message)
    parts: list[str] = []
    if media is not None:
        parts.append(_media_summary(media))
    structured = _structured_summary(message)
    if structured:
        parts.append(structured)
    if text:
        parts.append(text)
    return " ".join(parts) or tr("channel.telegram.reference_unavailable")


def _media_summary(media: TelegramMedia) -> str:
    return tr(
        "channel.telegram.media_summary",
        kind=tr(media.label_key),
        filename=media.filename,
        media_type=media.media_type,
    )


def _structured_summary(message: dict[str, Any]) -> str:
    sticker = message.get("sticker")
    if isinstance(sticker, dict):
        emoji = str(sticker.get("emoji") or "").strip()
        return tr(
            "channel.telegram.sticker",
            emoji=f" {emoji}" if emoji else "",
        )

    contact = message.get("contact")
    if isinstance(contact, dict):
        name = " ".join(
            part
            for part in (
                str(contact.get("first_name") or "").strip(),
                str(contact.get("last_name") or "").strip(),
            )
            if part
        )
        user_id = contact.get("user_id")
        return tr(
            "channel.telegram.contact",
            name=name or "-",
            phone=str(contact.get("phone_number") or "-"),
            user=user_id if isinstance(user_id, int) else "-",
        )

    venue = message.get("venue")
    if isinstance(venue, dict):
        location = venue.get("location")
        latitude, longitude = _coordinates(location)
        return tr(
            "channel.telegram.venue",
            title=str(venue.get("title") or "-"),
            address=str(venue.get("address") or "-"),
            latitude=latitude,
            longitude=longitude,
        )

    location = message.get("location")
    if isinstance(location, dict):
        latitude, longitude = _coordinates(location)
        return tr(
            "channel.telegram.location",
            latitude=latitude,
            longitude=longitude,
        )

    poll = message.get("poll")
    if isinstance(poll, dict):
        options = poll.get("options")
        option_texts = (
            [
                str(option.get("text") or "").strip()
                for option in options
                if isinstance(option, dict) and option.get("text")
            ]
            if isinstance(options, list)
            else []
        )
        return tr(
            "channel.telegram.poll",
            question=str(poll.get("question") or "-"),
            options=" / ".join(option_texts) or "-",
        )

    dice = message.get("dice")
    if isinstance(dice, dict):
        value = dice.get("value")
        return tr(
            "channel.telegram.dice",
            emoji=str(dice.get("emoji") or "🎲"),
            value=value if isinstance(value, int) else "-",
        )

    if isinstance(message.get("story"), dict):
        return tr("channel.telegram.story")
    return ""


def _reply_prefix(message: dict[str, Any]) -> str:
    quote = message.get("quote")
    quote_text = str(quote.get("text") or "").strip() if isinstance(quote, dict) else ""

    replied = message.get("reply_to_message")
    if isinstance(replied, dict):
        preview = quote_text or _message_preview(replied)
        return _reference_block(
            "channel.telegram.reply",
            _sender_label_for_message(replied),
            preview,
        )

    external = message.get("external_reply")
    if isinstance(external, dict):
        origin = external.get("origin")
        sender = _sender_label_for_origin(origin) if isinstance(origin, dict) else ""
        preview = quote_text or _message_preview(external)
        return _reference_block(
            "channel.telegram.external_reply",
            sender or tr("channel.telegram.sender_unknown"),
            preview,
        )

    if isinstance(message.get("reply_to_story"), dict):
        return _reference_block(
            "channel.telegram.reply",
            tr("channel.telegram.sender_unknown"),
            tr("channel.telegram.story"),
        )
    if quote_text:
        return _reference_block(
            "channel.telegram.reply",
            tr("channel.telegram.sender_unknown"),
            quote_text,
        )
    return ""


def _forward_prefix(message: dict[str, Any]) -> str:
    origin = message.get("forward_origin")
    if not isinstance(origin, dict) or message.get("is_automatic_forward"):
        return ""
    sender = _sender_label_for_origin(origin)
    if not sender:
        sender = tr("channel.telegram.sender_unknown")
    return tr("channel.telegram.forwarded_from", sender=sender)


def _reference_block(key: str, sender: str, preview: str) -> str:
    clipped = _truncate_preview(preview)
    quoted = "\n".join(f"> {line}" for line in clipped.splitlines())
    return tr(key, sender=sender, content=quoted)


def _truncate_preview(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(normalized) <= _REFERENCE_PREVIEW_LIMIT:
        return normalized
    return f"{normalized[: _REFERENCE_PREVIEW_LIMIT - 1].rstrip()}…"


def _sender_label_for_message(message: dict[str, Any]) -> str:
    sender = message.get("sender_chat")
    if not isinstance(sender, dict):
        sender = message.get("from")
    if not isinstance(sender, dict):
        return tr("channel.telegram.sender_unknown")
    return tr("channel.telegram.sender_identity", **_sender_fields(sender))


def _sender_label_for_origin(origin: dict[str, Any]) -> str:
    kind = str(origin.get("type") or "")
    if kind == "user" and isinstance(origin.get("sender_user"), dict):
        return tr(
            "channel.telegram.sender_identity",
            **_sender_fields(origin["sender_user"]),
        )
    if kind == "hidden_user":
        return str(origin.get("sender_user_name") or "").strip()
    payload = origin.get("sender_chat") if kind == "chat" else origin.get("chat")
    if isinstance(payload, dict):
        return tr("channel.telegram.sender_identity", **_sender_fields(payload))
    return ""


def _sender_fields(sender: dict[str, Any]) -> dict[str, Any]:
    sender_id = sender.get("id")
    username = str(sender.get("username") or "").strip()
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
    return {
        "sender": sender_id if isinstance(sender_id, int) else "?",
        "username": f"@{username}" if username else "-",
        "name": name or "-",
    }


def _coordinates(location: object) -> tuple[object, object]:
    if not isinstance(location, dict):
        return "-", "-"
    latitude = location.get("latitude")
    longitude = location.get("longitude")
    return (
        latitude if isinstance(latitude, int | float) else "-",
        longitude if isinstance(longitude, int | float) else "-",
    )
