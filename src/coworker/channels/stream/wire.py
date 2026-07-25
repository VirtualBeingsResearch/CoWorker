"""Wire-format helpers for the stream channel: outbound serialization.

Leaf module with no imports from the API or connection pool.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
from typing import Any

from coworker.core.types import CommunicateRequest
from coworker.i18n import tr

# Closure sentinel: enqueuing this wakes a blocked SSE/WS sender loop so it can
# exit and release the connection during shutdown. Namespaced to avoid collision
# with real messages.
SHUTDOWN_SENTINEL = "__coworker_shutdown__"
_MAX_ATTACHMENT_COUNT = 5
_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


def serialize_outbound_message(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, CommunicateRequest):
        payload = message.to_dict()
        if message.attachments:
            payload["attachments"] = _encode_attachments(message.attachments)
        return json.dumps(payload, ensure_ascii=False)
    return json.dumps(message, ensure_ascii=False)


def _encode_attachments(attachments: list[dict[str, Any]]) -> list[dict[str, str]]:
    if len(attachments) > _MAX_ATTACHMENT_COUNT:
        raise ValueError(tr("api.attachment.count_exceeded", limit=_MAX_ATTACHMENT_COUNT))
    encoded: list[dict[str, str]] = []
    for item in attachments:
        if not isinstance(item, dict):
            raise ValueError(tr("api.attachment.item_not_object"))
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError(tr("api.attachment.path_required"))
        path = Path(raw_path)
        if not path.is_file():
            raise ValueError(tr("api.attachment.missing", path=path))
        size = path.stat().st_size
        if size > _MAX_ATTACHMENT_BYTES:
            raise ValueError(
                tr(
                    "api.attachment.too_large",
                    name=path.name,
                    size=size,
                    limit=_MAX_ATTACHMENT_BYTES,
                )
            )
        filename = str(item.get("filename") or path.name)
        media_type = str(
            item.get("media_type")
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream"
        )
        encoded.append(
            {
                "filename": filename,
                "media_type": media_type,
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        )
    return encoded
