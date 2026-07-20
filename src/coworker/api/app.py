from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import tempfile
from datetime import datetime
from pathlib import Path as _Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import (
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles as _StaticFiles
from loguru import logger
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from coworker.api.admin import router as admin_router
from coworker.api.routes import (
    AttachmentSchema,
    _save_attachment,
    router,
    verify_communication_authorization,
)
from coworker.api.ws import SHUTDOWN_SENTINEL, ConnectionPool, serialize_outbound_message
from coworker.core.config import APIConfig, DesktopUpdatesConfig
from coworker.core.config_export import build_config_bundle, load_effective_config
from coworker.core.ids import new_compact_id
from coworker.core.types import CommunicateRequest, IncomingEvent
from coworker.version import __version__

if TYPE_CHECKING:
    from coworker.agent.event_collector import RuntimeEventCollector
    from coworker.agent.inbox_watcher import InboxWatcher
    from coworker.tools.communicate_tool import CommunicateTool

_api_defaults = APIConfig()
_cors_origins = [
    origin.strip()
    for origin in _api_defaults.cors_origins
    if origin.strip() and origin.strip() != "*"
]

app = FastAPI(title="Coworker API", version=__version__)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Accept", "Authorization", "Content-Type"],
)
app.include_router(router)
app.include_router(admin_router)

_pool = ConnectionPool()
_inbox: InboxWatcher | None = None
_communicate: CommunicateTool | None = None
_collector: RuntimeEventCollector | None = None
_desktop_updates_effective: DesktopUpdatesConfig | None = None
_desktop_updates_admin_token = ""
_shutting_down = False

_SSE_HEARTBEAT = 15.0  # 秒；无消息时发心跳注释保活，防代理 idle 超时
_DUPLICATE_CLOSE_CODE = 1008
_LOG_HISTORY_LIMIT_DEFAULT = 40
_LOG_HISTORY_LIMIT_MAX = 200
_LOG_HISTORY_DAYS_DEFAULT = 1
_LOG_HISTORY_DAYS_MAX = 30
_LOG_HISTORY_LINES_DEFAULT = 2000
_LOG_HISTORY_LINES_MAX = 20000


class CommunicateRegisterPayload(BaseModel):
    kind: str
    client_id: str
    display_name: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class DesktopReleasePayload(BaseModel):
    version: str
    notes: str = ""
    pub_date: str = ""


class DesktopPublishPayload(BaseModel):
    platforms: list[str] | None = None


_SEMVER_RE = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")
_PLATFORM_RE = re.compile(r"^(windows|linux|darwin)-(x86_64|i686|aarch64|armv7)$")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.+@()-]+")
_ARCH_FILENAME_ALIASES = {
    "x86_64": ("x86_64", "x64", "amd64", "intel"),
    "aarch64": ("aarch64", "arm64", "apple-silicon", "applesilicon"),
    "i686": ("i686", "x86"),
    "armv7": ("armv7", "armhf"),
}
_LATEST_FILE = "latest.json"
_RELEASE_FILE = "release.json"


def signal_shutdown() -> None:
    """进程关闭时调用：唤醒所有 SSE/WS 出站队列，让流式响应主动收尾、释放连接。
    否则它们阻塞在 queue.get() 直到心跳超时，会把 uvicorn 的优雅关闭拖到 graceful 超时
    （进而触发 CancelledError 噪声）。"""
    global _shutting_down
    _shutting_down = True
    queues: list[Any] = []
    if _communicate is not None:
        queues.extend(_communicate._ws_connections.values())
    queues.extend(_pool._outboxes.values())
    if _collector is not None:
        queues.extend(_collector.subscribers())  # /logs/stream SSE 订阅者
    for q in list(queues):
        try:
            q.put_nowait(SHUTDOWN_SENTINEL)
        except Exception:
            pass


def _format_sse(message: Any) -> str:
    """按 SSE 规范编码一条消息：每行加 `data: ` 前缀，以空行结尾。
    EventSource 收到后会用 `\\n` 自动重组多行 data。"""
    message = serialize_outbound_message(message)
    body = "".join(f"data: {line}\n" for line in message.split("\n"))
    return body + "\n"


def _connection_rejected_message(participant_id: str) -> str:
    return (
        f"连接被拒绝：'{participant_id}' 已有活跃的 SSE/WS 连接。"
        "同名连接按先到先得处理；请关闭已有连接后重试，或使用不同的 participant_id。"
    )


async def _reject_websocket(ws: WebSocket, participant_id: str) -> None:
    await ws.accept()
    await ws.send_text(_connection_rejected_message(participant_id))
    await ws.close(
        code=_DUPLICATE_CLOSE_CODE,
        reason="participant_id already connected",
    )


def _rejected_sse_response(participant_id: str) -> StreamingResponse:
    async def event_stream():
        yield "retry: 60000\n"
        yield _format_sse(_connection_rejected_message(participant_id))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "close",
            "X-Accel-Buffering": "no",
            "X-Connection-Rejected": "duplicate-participant",
        },
    )


def setup_ws(inbox: InboxWatcher, communicate: CommunicateTool) -> None:
    global _inbox, _communicate
    _inbox = inbox
    _communicate = communicate


def set_collector(collector: RuntimeEventCollector) -> None:
    """注入运行日志事件采集器（供 /api/logs/stream 实时推送与历史回放）。"""
    global _collector
    _collector = collector


def setup_desktop_updates(config: DesktopUpdatesConfig, admin_token: str = "") -> None:
    """Use the same effective config and admin credential as the management UI."""
    global _desktop_updates_effective, _desktop_updates_admin_token
    _desktop_updates_effective = config
    _desktop_updates_admin_token = admin_token


def _require_communicate() -> CommunicateTool:
    if _communicate is None:
        raise HTTPException(status_code=503, detail="Communicate tool not ready")
    return _communicate


def _desktop_updates_config() -> DesktopUpdatesConfig:
    return _desktop_updates_effective or DesktopUpdatesConfig()


def _updates_root() -> _Path:
    cfg = _desktop_updates_config()
    return _Path(cfg.dir)


def _release_dir(version: str) -> _Path:
    return _updates_root() / "releases" / version


def _release_path(version: str) -> _Path:
    return _release_dir(version) / _RELEASE_FILE


def _latest_path() -> _Path:
    return _updates_root() / _LATEST_FILE


def _normalize_version(value: str) -> str:
    value = value.strip()
    match = _SEMVER_RE.match(value)
    if not match:
        raise HTTPException(status_code=422, detail="version must be SemVer")
    return value.removeprefix("v")


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _SEMVER_RE.match(value.strip())
    if not match:
        return (0, 0, 0)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _is_newer(candidate: str, current: str) -> bool:
    return _version_tuple(candidate) > _version_tuple(current)


def _enqueue_desktop_update_checks(version: str) -> dict[str, int]:
    if _communicate is None:
        return {"eligible": 0, "enqueued": 0}

    desktops: dict[str, tuple[str, asyncio.Queue]] = {}
    for registration in _communicate.registration_records():
        metadata = registration.metadata
        capabilities = metadata.get("capabilities")
        if (
            registration.kind != "coworker-desktop"
            or not isinstance(capabilities, list)
            or "desktop_update_push" not in capabilities
        ):
            continue
        queue = _communicate.outbound_queue(registration.participant_id)
        desktop_id = str(metadata.get("desktop_id") or "").strip()
        current_version = str(metadata.get("desktop_version") or "").strip()
        if not desktop_id or (current_version and not _is_newer(version, current_version)):
            continue
        if queue is not None:
            desktops.setdefault(desktop_id, (registration.participant_id, queue))

    enqueued = 0
    for participant_id, queue in desktops.values():
        try:
            queue.put_nowait(
                CommunicateRequest(
                    participant_id=participant_id,
                    extra={
                        "operation": "check_desktop_update",
                        "request_id": new_compact_id("req_"),
                        "published_version": version,
                    },
                )
            )
            enqueued += 1
        except asyncio.QueueFull:
            logger.warning(f"Desktop update push queue is full: {participant_id}")
    return {"eligible": len(desktops), "enqueued": enqueued}


def _require_admin(authorization: str | None) -> None:
    tokens = tuple(
        token
        for token in (_desktop_updates_config().admin_token, _desktop_updates_admin_token)
        if token
    )
    if not tokens:
        raise HTTPException(status_code=503, detail="desktop update admin token is not configured")
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="missing bearer token")
    supplied = authorization[len(prefix):]
    if not any(secrets.compare_digest(supplied, token) for token in tokens):
        raise HTTPException(status_code=403, detail="invalid bearer token")


def _read_json_file(path: _Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="desktop release not found") from error
    except json.JSONDecodeError as error:
        raise HTTPException(
            status_code=500,
            detail=f"desktop release metadata is invalid: {error}",
        ) from error


def _write_json_atomic(path: _Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_release(version: str) -> dict[str, Any]:
    return _read_json_file(_release_path(_normalize_version(version)))


def _safe_filename(filename: str) -> str:
    leaf = filename.replace("\\", "/").split("/")[-1].strip(" .")
    safe = _SAFE_FILENAME_RE.sub("-", leaf).strip(" .-")
    if not safe:
        raise HTTPException(status_code=422, detail="asset filename is empty")
    return safe


def _filename_mentions_arch(filename: str, platform: str) -> bool:
    try:
        _, arch = platform.split("-", 1)
    except ValueError:
        return False
    compact = re.sub(r"[^a-z0-9]+", "", filename.lower())
    return any(
        re.sub(r"[^a-z0-9]+", "", alias.lower()) in compact
        for alias in _ARCH_FILENAME_ALIASES.get(arch, (arch,))
    )


def _stored_asset_filename(filename: str, platform: str, kind: str) -> str:
    safe = _safe_filename(filename)
    if kind == "updater" and platform.startswith("darwin-") and not _filename_mentions_arch(safe, platform):
        return _safe_filename(f"{platform}-{safe}")
    return safe


def _asset_path(version: str, filename: str) -> _Path:
    safe = _safe_filename(filename)
    root = (_release_dir(_normalize_version(version)) / "assets").resolve()
    path = (root / safe).resolve()
    if path.parent != root:
        raise HTTPException(status_code=400, detail="invalid asset path")
    return path


def _asset_url(request: Request, version: str, filename: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/desktop-updates/assets/{quote(version)}/{quote(filename)}"


def _summarize_release(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": data.get("version"),
        "notes": data.get("notes", ""),
        "pub_date": data.get("pub_date", ""),
        "published": bool(data.get("published")),
        "platforms": sorted((data.get("platforms") or {}).keys()),
        "installers": sorted((data.get("installers") or {}).keys()),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
    }


def _latest_response(latest: dict[str, Any], request: Request) -> dict[str, Any]:
    version = str(latest.get("version") or "")
    response = {**latest, "platforms": {}}
    for platform, asset in (latest.get("platforms") or {}).items():
        filename = str(asset.get("file") or "")
        response["platforms"][platform] = {
            "url": _asset_url(request, version, filename),
            "signature": str(asset.get("signature") or ""),
        }
    return response


def _publish_release(
    version: str,
    request: Request,
    platforms: list[str] | None = None,
) -> dict[str, Any]:
    version = _normalize_version(version)
    release = _read_release(version)
    platform_map = release.get("platforms") or {}
    selected = platforms or sorted(platform_map.keys())
    if not selected:
        raise HTTPException(status_code=422, detail="release has no platforms to publish")
    latest_platforms: dict[str, dict[str, str]] = {}

    publish_platforms = list(selected)
    if platforms is not None and _latest_path().exists():
        current_latest = _read_json_file(_latest_path())
        if str(current_latest.get("version") or "") == version:
            current_platforms = current_latest.get("platforms") or {}
            if isinstance(current_platforms, dict):
                selected_set = set(selected)
                preserved = sorted(
                    platform
                    for platform in current_platforms
                    if platform not in selected_set and platform in platform_map
                )
                publish_platforms = preserved + publish_platforms

    for platform in publish_platforms:
        if not _PLATFORM_RE.match(platform):
            raise HTTPException(status_code=422, detail=f"invalid platform: {platform}")
        asset = platform_map.get(platform)
        if not isinstance(asset, dict):
            raise HTTPException(status_code=422, detail=f"missing asset for platform: {platform}")
        filename = str(asset.get("file") or "")
        signature = str(asset.get("signature") or "").strip()
        path = _asset_path(version, filename)
        if not filename or not path.is_file():
            raise HTTPException(status_code=422, detail=f"missing file for platform: {platform}")
        if not signature:
            raise HTTPException(
                status_code=422,
                detail=f"missing signature for platform: {platform}",
            )
        latest_platforms[platform] = {
            "file": filename,
            "signature": signature,
        }

    now = datetime.now().astimezone().isoformat()
    latest = {
        "version": version,
        "notes": release.get("notes", ""),
        "pub_date": release.get("pub_date") or now,
        "platforms": latest_platforms,
    }
    _write_json_atomic(_latest_path(), latest)
    release["published"] = True
    release["updated_at"] = now
    _write_json_atomic(_release_path(version), release)
    response = _latest_response(latest, request)
    response["push"] = _enqueue_desktop_update_checks(version)
    return response


@app.post("/api/communicate/register")
async def communicate_register(
    payload: CommunicateRegisterPayload,
    authorization: str | None = Header(default=None),
):
    if payload.kind != "coworker-desktop":
        raise HTTPException(status_code=422, detail="only coworker-desktop registration is supported")
    verify_communication_authorization(authorization)
    versions = payload.metadata.get("protocol_versions", [])
    if not isinstance(versions, list) or 1 not in versions:
        raise HTTPException(
            status_code=422,
            detail="No compatible CoWorker Desktop protocol version; Coworker supports [1]",
        )
    communicate = _require_communicate()
    try:
        result = communicate.register_participant(
            kind=payload.kind,
            client_id=payload.client_id,
            display_name=payload.display_name,
            metadata=payload.metadata,
        )
        result["negotiated_protocol_version"] = 1
        return result
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/communicate/register")
async def communicate_registrations(authorization: str | None = Header(default=None)):
    verify_communication_authorization(authorization)
    return {"registrations": _require_communicate().list_registrations()}


@app.delete("/api/communicate/register/{registration_id}")
async def communicate_delete_registration(
    registration_id: str,
    authorization: str | None = Header(default=None),
):
    verify_communication_authorization(authorization)
    communicate = _require_communicate()
    try:
        return {"deleted": communicate.delete_registration(registration_id)}
    except KeyError as error:
        raise HTTPException(status_code=404, detail="registration not found") from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/desktop-updates/releases")
async def create_desktop_release(
    payload: DesktopReleasePayload,
    authorization: str | None = Header(default=None),
):
    _require_admin(authorization)
    version = _normalize_version(payload.version)
    path = _release_path(version)
    if path.exists():
        raise HTTPException(status_code=409, detail="desktop release already exists")
    now = datetime.now().astimezone().isoformat()
    release = {
        "version": version,
        "notes": payload.notes,
        "pub_date": payload.pub_date,
        "published": False,
        "created_at": now,
        "updated_at": now,
        "platforms": {},
        "installers": {},
    }
    _write_json_atomic(path, release)
    return release


@app.get("/api/desktop-updates/releases")
async def list_desktop_releases(authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    releases_root = _updates_root() / "releases"
    releases = []
    if releases_root.is_dir():
        for path in sorted(releases_root.glob(f"*/{_RELEASE_FILE}"), reverse=True):
            releases.append(_summarize_release(_read_json_file(path)))
    latest_version = None
    if _latest_path().exists():
        latest_version = _read_json_file(_latest_path()).get("version")
    return {"latest_version": latest_version, "releases": releases}


@app.get("/api/desktop-updates/releases/{version}")
async def get_desktop_release(version: str, authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    return _read_release(version)


@app.post("/api/desktop-updates/releases/{version}/assets")
async def upload_desktop_release_asset(
    version: str,
    platform: str = Form(...),
    signature: str = Form(""),
    kind: str = Form("updater"),
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    _require_admin(authorization)
    version = _normalize_version(version)
    if not _PLATFORM_RE.match(platform):
        raise HTTPException(status_code=422, detail="platform must be OS-ARCH")
    if kind not in {"updater", "installer"}:
        raise HTTPException(status_code=422, detail="kind must be updater or installer")
    release = _read_release(version)
    signature = signature.strip()
    if kind == "updater" and not signature:
        raise HTTPException(status_code=422, detail="signature is required")
    filename = _stored_asset_filename(file.filename or "", platform, kind)
    path = _asset_path(version, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="asset file is empty")
    path.write_bytes(content)
    asset = {
        "file": filename,
        "signature": signature,
        "kind": kind,
        "size": len(content),
        "uploaded_at": datetime.now().astimezone().isoformat(),
    }
    if kind == "updater":
        platforms = release.setdefault("platforms", {})
        platforms[platform] = asset
    else:
        installers = release.setdefault("installers", {})
        installers[platform] = asset
    release["updated_at"] = datetime.now().astimezone().isoformat()
    _write_json_atomic(_release_path(version), release)
    return release


@app.post("/api/desktop-updates/releases/{version}/publish")
async def publish_desktop_release(
    version: str,
    request: Request,
    payload: DesktopPublishPayload | None = None,
    authorization: str | None = Header(default=None),
):
    _require_admin(authorization)
    return _publish_release(version, request, payload.platforms if payload else None)


@app.post("/api/desktop-updates/releases/{version}/rollback")
async def rollback_desktop_release(
    version: str,
    request: Request,
    payload: DesktopPublishPayload | None = None,
    authorization: str | None = Header(default=None),
):
    _require_admin(authorization)
    return _publish_release(version, request, payload.platforms if payload else None)


@app.get("/api/desktop-updates/assets/{version}/{filename}")
async def download_desktop_release_asset(version: str, filename: str):
    path = _asset_path(version, filename)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="desktop update asset not found")
    return FileResponse(path)


@app.get("/api/desktop-updates/{target}/{arch}/{current_version}")
async def check_desktop_update(
    target: str,
    arch: str,
    current_version: str,
    request: Request,
):
    if not _latest_path().exists():
        return Response(status_code=204)
    latest = _read_json_file(_latest_path())
    latest_version = str(latest.get("version") or "")
    if not latest_version or not _is_newer(latest_version, current_version):
        return Response(status_code=204)
    platform = f"{target}-{arch}"
    asset = (latest.get("platforms") or {}).get(platform)
    if not isinstance(asset, dict):
        return Response(status_code=204)
    filename = str(asset.get("file") or "")
    if not filename:
        # Existing latest.json files stored an absolute URL. Recover the release
        # filename so address changes also work without republishing.
        release_asset = (_read_release(latest_version).get("platforms") or {}).get(platform)
        if isinstance(release_asset, dict):
            filename = str(release_asset.get("file") or "")
    url = _asset_url(request, latest_version, filename) if filename else str(asset.get("url") or "")
    signature = str(asset.get("signature") or "")
    if not url or not signature:
        return Response(status_code=204)
    return {
        "version": latest_version,
        "pub_date": latest.get("pub_date", ""),
        "url": url,
        "signature": signature,
        "notes": latest.get("notes", ""),
    }


@app.get("/api/export_config")
async def export_config(authorization: str | None = Header(default=None)):
    """把当前有效配置（含 data/ 全量、skills/palaces/subconscious、providers.json）
    打包成 zip 返回，供探索平台导入作为分支的起点。产物含密钥，只在本机/内网传输。"""
    _require_admin(authorization)
    config = load_effective_config()
    fd, tmp_name = tempfile.mkstemp(prefix="coworker-config-export-", suffix=".zip")
    os.close(fd)
    tmp_path = _Path(tmp_name)
    build_config_bundle(config, tmp_path)
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename="coworker-config-export.zip",
        background=BackgroundTask(tmp_path.unlink, missing_ok=True),
    )


@app.websocket("/ws/{participant_id}")
async def websocket_endpoint(ws: WebSocket, participant_id: str):
    if participant_id.startswith("codex-bridge:"):
        await ws.close(code=1008, reason="codex-bridge protocol is no longer supported")
        return
    if participant_id.startswith("coworker-desktop:"):
        try:
            verify_communication_authorization(ws.headers.get("authorization"))
        except HTTPException as error:
            await ws.close(code=1008, reason=str(error.detail))
            return
    queue: asyncio.Queue = asyncio.Queue()
    registered = False
    sender_task: asyncio.Task | None = None

    if _communicate:
        registered = _communicate.register_ws(
            participant_id,
            queue,
            transport="websocket",
        )
        if not registered:
            logger.info(f"WS rejected duplicate participant_id: {participant_id}")
            await _reject_websocket(ws, participant_id)
            return
    elif _pool.is_connected(participant_id):
        logger.info(f"WS rejected duplicate participant_id: {participant_id}")
        await _reject_websocket(ws, participant_id)
        return

    try:
        try:
            queue = await _pool.connect(participant_id, ws, queue)
        except ValueError:
            if registered and _communicate:
                _communicate.unregister_ws(participant_id, queue)
            logger.info(f"WS rejected duplicate participant_id: {participant_id}")
            await _reject_websocket(ws, participant_id)
            return

        sender_task = asyncio.create_task(_pool.run_sender(participant_id, queue, ws))
        while True:
            text = await ws.receive_text()
            if _inbox:
                from datetime import datetime
                content = text
                conversation_id = None
                attachments = []
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict) and any(
                        key in parsed for key in ("message", "conversation_id", "attachments")
                    ):
                        content = str(parsed.get("message") or "")
                        raw_conversation_id = parsed.get("conversation_id")
                        if isinstance(raw_conversation_id, str) and raw_conversation_id:
                            conversation_id = raw_conversation_id
                        for a in parsed.get("attachments", []):
                            attachments.append(_save_attachment(AttachmentSchema(**a)))
                except (json.JSONDecodeError, Exception):
                    pass
                await _inbox.push(IncomingEvent(
                    participant_id=participant_id,
                    content=content,
                    conversation_id=conversation_id,
                    timestamp=datetime.now(),
                    source="websocket",
                    attachments=attachments,
                ))
    except WebSocketDisconnect:
        logger.info(f"WS client disconnected: {participant_id}")
    finally:
        _pool.disconnect(participant_id, ws=ws, queue=queue)
        if registered and _communicate:
            _communicate.unregister_ws(participant_id, queue)
        if sender_task is not None:
            sender_task.cancel()


@app.get("/sse/{participant_id}")
async def sse_endpoint(
    participant_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """SSE 出站通道：与 WS 共用 CommunicateTool 的出站队列注册表，故 communicate
    工具投递逻辑零改动。入站方向用现有 POST /messages。EventSource 原生自动重连。"""
    if participant_id.startswith("codex-bridge:"):
        raise HTTPException(status_code=410, detail="codex-bridge protocol is no longer supported")
    if participant_id.startswith("coworker-desktop:"):
        verify_communication_authorization(authorization)
    queue: asyncio.Queue = asyncio.Queue()
    registered = False
    if _communicate:
        registered = _communicate.register_ws(
            participant_id,
            queue,
            transport="sse",
        )
        if not registered:
            logger.info(f"SSE rejected duplicate participant_id: {participant_id}")
            return _rejected_sse_response(participant_id)
    logger.info(f"SSE connected: {participant_id}")

    async def event_stream():
        try:
            yield ": connected\n\n"  # 立即开流，便于代理/客户端确认连接已建立
            while True:
                if await request.is_disconnected() or _shutting_down:
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT)
                except TimeoutError:
                    yield ": ping\n\n"  # 心跳注释保活
                    continue
                except asyncio.CancelledError:
                    break
                if msg == SHUTDOWN_SENTINEL:  # 关闭哨兵：立即收尾，尽快释放连接
                    break
                yield _format_sse(msg)
        finally:
            if registered and _communicate:
                _communicate.unregister_ws(participant_id, queue)
            logger.info(f"SSE disconnected: {participant_id}")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关掉 nginx 缓冲，保证即时推送
        },
    )


@app.get("/logs/stream")
async def logs_stream(
    request: Request,
    history_limit: int = Query(
        _LOG_HISTORY_LIMIT_DEFAULT,
        ge=0,
        le=_LOG_HISTORY_LIMIT_MAX,
        description="连接建立时回放的最多展示事件数；0 表示不回放历史。",
    ),
    history_days: int = Query(
        _LOG_HISTORY_DAYS_DEFAULT,
        ge=1,
        le=_LOG_HISTORY_DAYS_MAX,
        description="历史回放只保留最近多少天的原始日志。",
    ),
    history_lines: int = Query(
        _LOG_HISTORY_LINES_DEFAULT,
        ge=0,
        le=_LOG_HISTORY_LINES_MAX,
        description="历史回放最多从原始日志尾部读取多少行；0 表示不读历史。",
    ),
):
    """运行日志 SSE 流（/logs/stream）：身份证背面「运行日志」的数据源。

    数据来自 RuntimeEventCollector——InteractionLogger 的唯一 tap，故事件流与持久化日志
    （data/logs/interactions*.jsonl）天然一致。先回放最近若干条历史事件，再实时推送。
    收尾机制与 /sse/{id} 一致（哨兵 + is_disconnected + 心跳），不拖住 uvicorn 优雅关闭。"""
    if _collector is None:
        return StreamingResponse(
            iter([": collector-not-ready\n\n"]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    collector = _collector
    queue = collector.register()
    logger.info("runtime-log SSE connected")

    async def event_stream():
        try:
            yield ": connected\n\n"  # 立即开流，便于代理/客户端确认连接已建立
            # 历史回放：新连接先补最近的运行上下文（在订阅之后取，故不会漏掉这期间的实时事件）
            for ev in collector.recent(
                history_limit,
                days=history_days,
                tail_lines=history_lines,
            ):
                yield _format_sse(json.dumps(ev, ensure_ascii=False))
            while True:
                if await request.is_disconnected() or _shutting_down:
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT)
                except TimeoutError:
                    yield ": ping\n\n"  # 心跳注释保活
                    continue
                if ev == SHUTDOWN_SENTINEL:  # 关闭哨兵：立即收尾，尽快释放连接
                    break
                yield _format_sse(json.dumps(ev, ensure_ascii=False))
        finally:
            collector.unregister(queue)
            logger.info("runtime-log SSE disconnected")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关掉 nginx 缓冲，保证即时推送
        },
    )


# ---- 前端静态文件托管 ----
_WEB_DIST = _Path(__file__).resolve().parent.parent / "web"
if _WEB_DIST.is_dir():
    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    async def admin_spa_entry():
        """管理控制台是前端 SPA 子路由，显式回退到同一份 index.html。"""
        return FileResponse(_WEB_DIST / "index.html")

    app.mount("/", _StaticFiles(directory=str(_WEB_DIST), html=True), name="web-static")
