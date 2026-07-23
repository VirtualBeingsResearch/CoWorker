from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coworker.core.ids import new_compact_id
from coworker.core.types import AttachmentData


@dataclass(frozen=True)
class InboundEnvelope:
    """Raw protocol payload handed from an API adapter to a Channel."""

    participant_id: str
    source: str
    payload: Any


class AttachmentStore:
    """Persist channel-provided base64 attachments at the channel boundary."""

    _IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    _PDF_MEDIA_TYPES = {"application/pdf"}
    _UNSAFE_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def save(self, raw: dict[str, Any], *, keep_inline_data: bool) -> AttachmentData:
        filename = str(raw.get("filename") or "attachment")
        media_type = str(raw.get("media_type") or "application/octet-stream")
        data = str(raw.get("data") or "")
        decoded = base64.b64decode(data)
        leaf = re.split(r"[\\/]+", filename)[-1].strip(" .")
        safe_name = self._UNSAFE_CHARS_RE.sub("-", leaf).strip(" .-") or "attachment"
        self._root.mkdir(parents=True, exist_ok=True)
        destination = self._root / f"{new_compact_id()}_{safe_name}"
        destination.write_bytes(decoded)
        keep_data = keep_inline_data and (
            media_type in self._IMAGE_MEDIA_TYPES or media_type in self._PDF_MEDIA_TYPES
        )
        return AttachmentData(
            filename=safe_name,
            media_type=media_type,
            saved_path=str(destination),
            data=data if keep_data else None,
        )
