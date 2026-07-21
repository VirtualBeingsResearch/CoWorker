from __future__ import annotations

from coworker.core.types import IncomingEvent
from coworker.i18n import tr

# 把 IncomingEvent.source 翻译成给模型看的人类可读来源标签，
# 让模型知道每条消息从哪个信道进来（影响回复路由 / 语气 / 附件是否可用）。
# 仅信道类来源（有真实发送方、回复需选信道）才会用到这些标签。
_SOURCE_LABEL_KEYS: dict[str, str] = {
    "file": "source.file",
    "rest": "source.rest",
    "websocket": "source.websocket",
    "wecom": "source.wecom",
    "coworker_desktop": "source.coworker_desktop",
    "bubble": "source.bubble",
    "codex": "source.codex",
}

# 内容已自带 [闹钟提醒]/[代码任务完成] 等自描述前缀的来源：原样透传，
# 不再套「[来自X][participant]的消息:」外壳，避免三重冗余。
_SELF_DESCRIBING_SOURCES = {"alarm", "code_job", "task_reminder"}

# 系统通知：participant_id 是占位符，但 content 不一定自带来源标记
# （如「记忆树回溯完成…」「图片分析结果…」），统一加 [系统] 前缀以标明来自系统。
_SYSTEM_SOURCES = {"system", "compress_memory"}


def format_event_text(event: IncomingEvent) -> str:
    if event.source in _SELF_DESCRIBING_SOURCES:
        return event.content
    if event.source in _SYSTEM_SOURCES:
        return tr("incoming.system", content=event.content)
    source_key = _SOURCE_LABEL_KEYS.get(event.source)
    source_label = tr(source_key) if source_key else event.source
    conversation_label = f"[conversation:{event.conversation_id}]" if event.conversation_id else ""
    return tr(
        "incoming.message",
        source=source_label,
        participant=event.participant_id,
        conversation=conversation_label,
        content=event.content,
    )


def build_content_blocks(events: list[IncomingEvent]) -> str | list[dict]:
    """Build model content for one or more inbound events.

    Both the main loop and a participant-bound bubble use this path so a direct
    handoff preserves the original sender, conversation id, and attachments.
    """
    if len(events) == 1 and not events[0].attachments:
        return format_event_text(events[0])

    blocks: list[dict] = []
    for event in events:
        if event.content or event.attachments:
            blocks.append({"type": "text", "text": format_event_text(event)})
        for att in event.attachments:
            if att.media_type.startswith("image/") and att.data is not None:
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": att.media_type,
                            "data": att.data,
                        },
                        "_filename": att.filename,
                        "_saved_path": att.saved_path,
                    }
                )
            elif att.media_type == "application/pdf" and att.data is not None:
                blocks.append(
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": att.media_type,
                            "data": att.data,
                        },
                        "_filename": att.filename,
                        "_saved_path": att.saved_path,
                    }
                )
            else:
                attachment_kind = tr(
                    "incoming.video_attachment"
                    if att.media_type.startswith("video/")
                    else "incoming.attachment"
                )
                blocks.append(
                    {
                        "type": "text",
                        "text": tr(
                            "incoming.saved_attachment",
                            kind=attachment_kind,
                            filename=att.filename,
                            path=att.saved_path,
                        ),
                    }
                )

    return blocks
