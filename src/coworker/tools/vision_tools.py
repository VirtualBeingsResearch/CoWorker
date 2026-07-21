from __future__ import annotations

import asyncio
import base64
import io
import mimetypes
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from loguru import logger
from PIL import Image

from coworker.core.tool_scope import ToolScope
from coworker.core.types import IncomingEvent, Message, ToolResult
from coworker.i18n import capture_locale, locale_context, tr
from coworker.tools.base import Tool, ToolDefinition

if TYPE_CHECKING:
    from coworker.agent.inbox_watcher import InboxWatcher
    from coworker.brain.brain import Brain

_SUPPORTED_IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_SUPPORTED_VIDEO_MEDIA_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
    "video/webm",
    "video/x-matroska",
    "video/x-flv",
    "video/x-ms-wmv",
}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".webm", ".mkv", ".flv", ".wmv"}
_VIDEO_BASE64_LIMIT = 10 * 1024 * 1024
_VIDEO_SOURCE_LIMIT = 100 * 1024 * 1024
_DEFAULT_MAX_DIMENSION = 960


def _resize_image(
    raw: bytes, media_type: str, max_dimension: int = _DEFAULT_MAX_DIMENSION
) -> tuple[bytes, str, str]:
    """等比缩放图片，使长边不超过 max_dimension。"""
    img = Image.open(io.BytesIO(raw))
    w, h = img.size
    if max(w, h) <= max_dimension:
        return raw, media_type, ""

    scale = max_dimension / max(w, h)
    new_w, new_h = int(w * scale), int(h * scale)
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    out = io.BytesIO()
    fmt = "JPEG" if media_type == "image/jpeg" else "PNG"
    out_media_type = "image/jpeg" if fmt == "JPEG" else "image/png"
    resized.save(out, format=fmt)
    return (
        out.getvalue(),
        out_media_type,
        tr(
            "vision.resized",
            width=w,
            height=h,
            new_width=new_w,
            new_height=new_h,
        ),
    )


def _sniff_image_media_type(raw: bytes) -> str | None:
    try:
        with Image.open(io.BytesIO(raw)) as img:
            return {
                "JPEG": "image/jpeg",
                "PNG": "image/png",
                "GIF": "image/gif",
                "WEBP": "image/webp",
            }.get(img.format or "")
    except Exception:
        return None


def _detect_media_type(raw: bytes, source: str, declared: str = "") -> str | None:
    declared = declared.split(";", 1)[0].strip().lower()
    if declared in _SUPPORTED_IMAGE_MEDIA_TYPES | _SUPPORTED_VIDEO_MEDIA_TYPES:
        return declared
    guessed, _ = mimetypes.guess_type(urlparse(source).path)
    if guessed in _SUPPORTED_IMAGE_MEDIA_TYPES | _SUPPORTED_VIDEO_MEDIA_TYPES:
        return guessed
    return _sniff_image_media_type(raw)


def _video_data_url_size(raw: bytes, media_type: str) -> int:
    prefix_size = len(f"data:{media_type};base64,".encode())
    encoded_size = 4 * ((len(raw) + 2) // 3)
    return prefix_size + encoded_size


async def _compress_video(raw: bytes, original_suffix: str) -> bytes:
    suffix = original_suffix.lower() if original_suffix.lower() in _VIDEO_SUFFIXES else ".bin"
    with tempfile.TemporaryDirectory(prefix="coworker-video-") as temp_dir:
        input_path = Path(temp_dir) / f"input{suffix}"
        output_path = Path(temp_dir) / "compressed.mp4"
        input_path.write_bytes(raw)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-loglevel",
                "error",
                "-i",
                str(input_path),
                "-vf",
                "scale=min(1280\\,iw):-2",
                "-c:v",
                "libx264",
                "-crf",
                "28",
                "-preset",
                "veryfast",
                "-an",
                "-movflags",
                "+faststart",
                "-y",
                str(output_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise RuntimeError(tr("vision.ffmpeg_missing")) from e

        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except TimeoutError as e:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(tr("vision.ffmpeg_timeout")) from e
        if proc.returncode != 0 or not output_path.is_file():
            detail = (stderr or b"").decode(errors="replace").strip()
            raise RuntimeError(
                tr("vision.ffmpeg_failed", error=detail or tr("vision.unknown_error"))
            )
        return output_path.read_bytes()


async def _prepare_video(raw: bytes, media_type: str, filename: str) -> tuple[bytes, str, str]:
    if _video_data_url_size(raw, media_type) < _VIDEO_BASE64_LIMIT:
        return raw, media_type, ""

    compressed = await _compress_video(raw, Path(filename).suffix)
    if len(compressed) >= len(raw):
        compressed = raw
        compressed_media_type = media_type
    else:
        compressed_media_type = "video/mp4"
    if _video_data_url_size(compressed, compressed_media_type) >= _VIDEO_BASE64_LIMIT:
        raise RuntimeError(tr("vision.compressed_too_large"))
    return (
        compressed,
        compressed_media_type,
        tr("vision.compressed", before=len(raw), after=len(compressed)),
    )


async def _load_media(media_path: str) -> tuple[bytes, str, str]:
    declared_type = ""
    if media_path.startswith(("http://", "https://")):
        try:
            import httpx

            async with httpx.AsyncClient(timeout=30) as client:
                async with client.stream("GET", media_path) as resp:
                    resp.raise_for_status()
                    declared_type = resp.headers.get("content-type", "")
                    declared_base = declared_type.split(";", 1)[0].strip().lower()
                    guessed_type, _ = mimetypes.guess_type(urlparse(media_path).path)
                    video_hint = (
                        declared_base in _SUPPORTED_VIDEO_MEDIA_TYPES
                        or guessed_type in _SUPPORTED_VIDEO_MEDIA_TYPES
                    )
                    chunks: list[bytes] = []
                    downloaded = 0
                    async for chunk in resp.aiter_bytes():
                        downloaded += len(chunk)
                        if video_hint and downloaded > _VIDEO_SOURCE_LIMIT:
                            raise ValueError(tr("vision.source_too_large_download"))
                        chunks.append(chunk)
                    raw = b"".join(chunks)
        except Exception as e:
            raise RuntimeError(tr("vision.download_failed", error=e)) from e
        filename = Path(urlparse(media_path).path).name or "media"
    else:
        path = Path(media_path)
        if not path.exists():
            raise FileNotFoundError(tr("vision.file_missing", path=media_path))
        if not path.is_file():
            raise ValueError(tr("vision.not_file", path=media_path))
        raw = path.read_bytes()
        filename = path.name

    media_type = _detect_media_type(raw, media_path, declared_type)
    if media_type is None:
        raise ValueError(tr("vision.unsupported_media", filename=filename))
    if media_type in _SUPPORTED_VIDEO_MEDIA_TYPES and len(raw) > _VIDEO_SOURCE_LIMIT:
        raise ValueError(tr("vision.source_too_large_process"))
    return raw, media_type, filename


class ViewImageTool(Tool):
    """让视觉模型主动加载图片到对话上下文中直接查看。"""

    vision_model_only = True

    def __init__(self, max_dimension: int = _DEFAULT_MAX_DIMENSION) -> None:
        self._max_dimension = max_dimension

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="view_image",
            description=(
                "加载本地文件路径或 HTTP(S) URL 的图片，直接在对话中查看图片内容。"
                "适用于需要主动查看截图、图表、UI 图片等场景。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "图片的本地文件路径（如 /tmp/screenshot.png）或 HTTP(S) URL",
                    },
                    "full_resolution": {
                        "type": "boolean",
                        "description": "是否使用原始分辨率，不进行缩放（默认 false）",
                        "default": False,
                    },
                },
                "required": ["image_path"],
            },
        )

    async def execute(
        self, image_path: str, full_resolution: bool = False, **_: object
    ) -> ToolResult:
        try:
            raw, media_type, filename = await _load_media(image_path)
        except Exception as e:
            return ToolResult(tool_call_id="", content=str(e), is_error=True)
        if media_type not in _SUPPORTED_IMAGE_MEDIA_TYPES:
            return ToolResult(
                tool_call_id="",
                content=tr("vision.unsupported_image", filename=filename),
                is_error=True,
            )

        try:
            raw, media_type, resize_note = (
                (raw, media_type, "")
                if full_resolution
                else _resize_image(raw, media_type, self._max_dimension)
            )
        except Exception as e:
            return ToolResult(
                tool_call_id="",
                content=tr("vision.image_read_failed", error=e),
                is_error=True,
            )
        note = tr("vision.parenthesized_note", note=resize_note) if resize_note else ""
        loaded = tr("vision.image_loaded", filename=filename, note=note)
        content_blocks: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(raw).decode(),
                },
                "_filename": filename,
            },
            {"type": "text", "text": loaded},
        ]
        return ToolResult(
            tool_call_id="",
            content=loaded,
            content_blocks=content_blocks,
        )


class VisualAnalysisTool(Tool):
    def __init__(
        self,
        brain: Brain,
        vision_provider: str = "",
        vision_model: str = "",
        inbox: InboxWatcher | None = None,
        max_dimension: int = _DEFAULT_MAX_DIMENSION,
    ) -> None:
        self._brain = brain
        self._vision_provider = vision_provider
        self._vision_model = vision_model
        self._inbox = inbox
        self._max_dimension = max_dimension

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="visual_analyze",
            description=(
                "对图片或视频进行视觉分析和推理。当你收到视觉附件但无法直接查看时使用此工具。"
                "支持本地文件路径和 HTTP(S) URL；视频会作为 Base64 文件直接交给支持原生视频"
                "输入的视觉模型，超限时会尝试压缩。调用后立即返回，结果通过系统消息推送。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "media_path": {
                        "type": "string",
                        "description": "图片或视频的本地文件路径或 HTTP(S) URL",
                    },
                    "question": {
                        "type": "string",
                        "description": "关于图片或视频需要分析、识别或推理的具体问题",
                    },
                },
                "required": ["media_path", "question"],
            },
        )

    def _configured_vision(self) -> tuple[str, str]:
        provider = getattr(self._brain, "vision_provider_name", "")
        model = getattr(self._brain, "vision_model", "")
        if not isinstance(provider, str):
            provider = ""
        if not isinstance(model, str):
            model = ""
        return provider or self._vision_provider, model or self._vision_model

    async def execute(self, media_path: str, question: str, **_: object) -> ToolResult:
        vision_provider, vision_model = self._configured_vision()
        if not vision_provider or not vision_model:
            return ToolResult(
                tool_call_id="",
                content=tr("vision.config_missing"),
                is_error=True,
            )
        inbox = self._inbox
        if inbox is None:
            return ToolResult(tool_call_id="", content=tr("vision.inbox_unready"), is_error=True)

        try:
            raw, media_type, filename = await _load_media(media_path)
        except Exception as e:
            return ToolResult(tool_call_id="", content=str(e), is_error=True)

        is_video = media_type in _SUPPORTED_VIDEO_MEDIA_TYPES
        if not is_video:
            try:
                raw, media_type, resize_note = _resize_image(raw, media_type, self._max_dimension)
            except Exception as e:
                return ToolResult(
                    tool_call_id="",
                    content=tr("vision.image_read_failed", error=e),
                    is_error=True,
                )
        else:
            resize_note = ""

        bound_locale = capture_locale()

        async def _run() -> None:
            with locale_context(bound_locale):
                kind = tr("vision.video" if is_video else "vision.image")
                try:
                    prepared_raw = raw
                    prepared_media_type = media_type
                    preparation_note = resize_note
                    if is_video:
                        prepared_raw, prepared_media_type, preparation_note = await _prepare_video(
                            raw, media_type, filename
                        )
                    prompt = question
                    if preparation_note:
                        prompt = tr(
                            "vision.preparation_note",
                            question=question,
                            kind=kind,
                            note=preparation_note,
                        )
                    media_block: dict = {
                        "type": "video" if is_video else "image",
                        "source": {
                            "type": "base64",
                            "media_type": prepared_media_type,
                            "data": base64.standard_b64encode(prepared_raw).decode(),
                        },
                        "_filename": filename,
                    }
                    messages = [
                        Message(
                            role="user",
                            content=[media_block, {"type": "text", "text": prompt}],
                        )
                    ]
                    answer = await self._brain.query_with_vision(
                        messages,
                        vision_provider=vision_provider,
                        vision_model=vision_model,
                        usage_context={"label": filename},
                        require_video=is_video,
                    )
                    content = tr(
                        "vision.analysis_result",
                        kind=kind,
                        path=media_path,
                        question=question,
                        answer=answer,
                    )
                except Exception as e:
                    logger.error(f"VisualAnalysisTool background task failed: {e}")
                    content = tr("vision.analysis_failed", kind=kind, path=media_path, error=e)
                await inbox.push(
                    IncomingEvent(participant_id="system", content=content, source="system")
                )

        task = asyncio.create_task(_run())
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        kind = tr("vision.video" if is_video else "vision.image")
        return ToolResult(tool_call_id="", content=tr("vision.analysis_started", kind=kind))

    def fork(self, scope: ToolScope) -> VisualAnalysisTool:
        return VisualAnalysisTool(
            brain=scope.brain or self._brain,
            inbox=scope.inbox,
            max_dimension=self._max_dimension,
        )
