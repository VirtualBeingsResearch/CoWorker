"""Attachment filenames that stay inside the attachments directory."""

from __future__ import annotations

import re
from pathlib import Path

_MAX_LENGTH = 180
_MAX_SUFFIX = 20
# 通道文件名来自对方的消息内容：Windows 拒绝这些字符，分隔符则可能把文件写到
# 附件目录之外，控制字符还会破坏日志与展示。
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SEPARATORS = re.compile(r"[\\/]+")


def safe_attachment_filename(value: str, *, fallback: str) -> str:
    """Return a portable basename for a channel-supplied filename.

    Keeps only the last path segment, replaces characters that are illegal on
    Windows, and clamps the length while preserving a short suffix.
    """
    name = _SEPARATORS.split(str(value or ""))[-1]
    name = _ILLEGAL.sub("_", name).strip(" .")
    if not name:
        return fallback
    if len(name) <= _MAX_LENGTH:
        return name
    suffix = Path(name).suffix[:_MAX_SUFFIX]
    return f"{Path(name).stem[:_MAX_LENGTH - len(suffix)]}{suffix}"
