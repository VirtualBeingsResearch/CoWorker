from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import tempfile
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
from coworker.desktop_updates import (
    DesktopReleaseStore,
    DesktopReleaseStoreError,
    InvalidReleaseDataError,
    ReleaseExistsError,
    ReleaseNotFoundError,
    SemVer,
    SemVerError,
    UnsafePathError,
    normalize_version,
)
from coworker.i18n import tr
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
_desktop_release_store_effective: DesktopReleaseStore | None = None
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
    return tr("api.desktop.connection_rejected", participant=participant_id)


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


def setup_desktop_updates(
    config: DesktopUpdatesConfig,
    admin_token: str = "",
    store: DesktopReleaseStore | None = None,
) -> None:
    """Use the same effective config, store, and admin credential as the management UI."""
    global _desktop_updates_effective, _desktop_updates_admin_token, _desktop_release_store_effective
    _desktop_updates_effective = config
    _desktop_updates_admin_token = admin_token
    _desktop_release_store_effective = store or DesktopReleaseStore(config.dir)


def _require_communicate() -> CommunicateTool:
    if _communicate is None:
        raise HTTPException(
            status_code=503, detail=tr("api.state.communicate_tool_not_ready")
        )
    return _communicate


def _desktop_updates_config() -> DesktopUpdatesConfig:
    return _desktop_updates_effective or DesktopUpdatesConfig()


def _desktop_release_store() -> DesktopReleaseStore:
    global _desktop_release_store_effective
    configured_root = _Path(_desktop_updates_config().dir)
    if (
        _desktop_release_store_effective is None
        or _desktop_release_store_effective.root.resolve() != configured_root.resolve()
    ):
        _desktop_release_store_effective = DesktopReleaseStore(configured_root)
    return _desktop_release_store_effective


def _normalize_version(value: str) -> str:
    try:
        return normalize_version(value)
    except (SemVerError, UnsafePathError) as error:
        raise HTTPException(status_code=422, detail=tr("api.desktop.version_semver")) from error


def _is_newer(candidate: str, current: str) -> bool:
    try:
        candidate_version = SemVer.parse(candidate)
    except SemVerError:
        return False
    try:
        current_version = SemVer.parse(current)
    except SemVerError:
        return True
    return candidate_version > current_version


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
        raise HTTPException(
            status_code=503, detail=tr("api.auth.desktop_token_unconfigured")
        )
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail=tr("api.auth.bearer_token_missing"))
    supplied = authorization[len(prefix):]
    if not any(secrets.compare_digest(supplied, token) for token in tokens):
        raise HTTPException(status_code=403, detail=tr("api.auth.bearer_token_invalid"))


def _desktop_store_http_error(error: DesktopReleaseStoreError) -> HTTPException:
    if isinstance(error, ReleaseNotFoundError):
        return HTTPException(status_code=404, detail=tr("api.desktop.release_missing"))
    if isinstance(error, ReleaseExistsError):
        return HTTPException(status_code=409, detail=tr("api.desktop.release_exists"))
    if isinstance(error, UnsafePathError):
        return HTTPException(status_code=400, detail=tr("api.desktop.asset_path_invalid"))
    if isinstance(error, InvalidReleaseDataError):
        return HTTPException(status_code=422, detail=str(error))
    return HTTPException(status_code=500, detail=str(error))


def _asset_path(version: str, filename: str) -> _Path:
    try:
        return _desktop_release_store().asset_path(_normalize_version(version), filename)
    except DesktopReleaseStoreError as error:
        raise _desktop_store_http_error(error) from error


def _asset_url(request: Request, version: str, filename: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/desktop-updates/assets/{quote(version)}/{quote(filename)}"


def _feed_asset_url(version: str, filename: str) -> str:
    return f"/api/desktop-updates/assets/{quote(version)}/{quote(filename)}"


def _manifest_url(version: str) -> str:
    return f"/api/desktop-updates/feed/v1/releases/{quote(version)}"


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
        "source": data.get("source"),
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


def _require_feed_access(authorization: str | None) -> None:
    token = _desktop_updates_config().feed_token
    if not token:
        raise HTTPException(status_code=404, detail=tr("api.desktop.feed_disabled"))
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail=tr("api.auth.invalid_bearer"))
    supplied = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied, token):
        raise HTTPException(status_code=403, detail=tr("api.auth.forbidden"))


def _published_release_summary(release: dict[str, Any]) -> dict[str, Any]:
    version = str(release.get("version") or "")
    source = release.get("source") if isinstance(release.get("source"), dict) else {}
    prerelease = bool(source.get("prerelease")) if isinstance(source, dict) else False
    return {
        "id": version,
        "version": version,
        "notes": str(release.get("notes") or ""),
        "pub_date": str(release.get("pub_date") or release.get("updated_at") or ""),
        "prerelease": prerelease,
        "manifest_url": _manifest_url(version),
    }


async def _published_release_manifest(release: dict[str, Any]) -> dict[str, Any]:
    version = str(release.get("version") or "")
    source = release.get("source") if isinstance(release.get("source"), dict) else {}
    prerelease = bool(source.get("prerelease")) if isinstance(source, dict) else False
    assets: list[dict[str, Any]] = []
    for collection_name in ("platforms", "installers"):
        collection = release.get(collection_name) or {}
        if not isinstance(collection, dict):
            continue
        for platform, asset in sorted(collection.items()):
            if not isinstance(asset, dict):
                continue
            filename = str(asset.get("file") or "")
            if not filename:
                continue
            sha256 = str(asset.get("sha256") or "").lower()
            if not sha256:
                sha256 = await _desktop_release_store().asset_sha256(version, filename)
            kind = str(asset.get("kind") or ("updater" if collection_name == "platforms" else "installer"))
            assets.append(
                {
                    "platform": str(platform),
                    "kind": kind,
                    "file": filename,
                    "size": int(asset.get("size") or _asset_path(version, filename).stat().st_size),
                    "sha256": sha256,
                    "signature": str(asset.get("signature") or ""),
                    "download_url": _feed_asset_url(version, filename),
                }
            )
    revision_digest = hashlib.sha256(
        json.dumps(assets, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": 1,
        "release": {
            "id": version,
            "version": version,
            "notes": str(release.get("notes") or ""),
            "pub_date": str(release.get("pub_date") or release.get("updated_at") or ""),
            "prerelease": prerelease,
            "revision": f"sha256:{revision_digest}",
            "assets": assets,
        },
    }


async def _publish_release(
    version: str,
    request: Request,
    platforms: list[str] | None = None,
) -> dict[str, Any]:
    version = _normalize_version(version)
    try:
        result = await _desktop_release_store().publish_release(version, platforms)
    except DesktopReleaseStoreError as error:
        raise _desktop_store_http_error(error) from error
    response = _latest_response(result["latest"], request)
    response["push"] = _enqueue_desktop_update_checks(version)
    return response


@app.post("/api/communicate/register")
async def communicate_register(
    payload: CommunicateRegisterPayload,
    authorization: str | None = Header(default=None),
):
    if payload.kind != "coworker-desktop":
        raise HTTPException(
            status_code=422,
            detail=tr("api.desktop.registration_kind_unsupported"),
        )
    verify_communication_authorization(authorization)
    versions = payload.metadata.get("protocol_versions", [])
    if not isinstance(versions, list) or 1 not in versions:
        raise HTTPException(
            status_code=422,
            detail=tr("api.desktop.protocol_incompatible"),
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
        raise HTTPException(
            status_code=404, detail=tr("api.desktop.registration_missing")
        ) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/desktop-updates/releases")
async def create_desktop_release(
    payload: DesktopReleasePayload,
    authorization: str | None = Header(default=None),
):
    _require_admin(authorization)
    try:
        return await _desktop_release_store().create_release(
            _normalize_version(payload.version),
            notes=payload.notes,
            pub_date=payload.pub_date,
        )
    except DesktopReleaseStoreError as error:
        raise _desktop_store_http_error(error) from error


@app.get("/api/desktop-updates/releases")
async def list_desktop_releases(authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    try:
        release_data = await _desktop_release_store().list_releases()
        latest = await _desktop_release_store().read_latest()
    except DesktopReleaseStoreError as error:
        raise _desktop_store_http_error(error) from error
    return {
        "latest_version": latest.get("version") if latest else None,
        "releases": [_summarize_release(release) for release in release_data],
    }


@app.get("/api/desktop-updates/releases/{version}")
async def get_desktop_release(version: str, authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    try:
        return await _desktop_release_store().read_release(_normalize_version(version))
    except DesktopReleaseStoreError as error:
        raise _desktop_store_http_error(error) from error


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
    content = await file.read()
    try:
        return await _desktop_release_store().upload_asset(
            _normalize_version(version),
            platform=platform,
            signature=signature,
            kind=kind,
            filename=file.filename or "",
            content=content,
        )
    except DesktopReleaseStoreError as error:
        raise _desktop_store_http_error(error) from error


@app.post("/api/desktop-updates/releases/{version}/publish")
async def publish_desktop_release(
    version: str,
    request: Request,
    payload: DesktopPublishPayload | None = None,
    authorization: str | None = Header(default=None),
):
    _require_admin(authorization)
    return await _publish_release(version, request, payload.platforms if payload else None)


@app.post("/api/desktop-updates/releases/{version}/rollback")
async def rollback_desktop_release(
    version: str,
    request: Request,
    payload: DesktopPublishPayload | None = None,
    authorization: str | None = Header(default=None),
):
    _require_admin(authorization)
    return await _publish_release(version, request, payload.platforms if payload else None)


@app.get("/api/desktop-updates/feed/v1/releases")
async def list_desktop_release_feed(
    limit: int = Query(default=20, ge=1, le=100),
    authorization: str | None = Header(default=None),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
):
    _require_feed_access(authorization)
    try:
        releases = await _desktop_release_store().list_releases()
    except DesktopReleaseStoreError as error:
        raise _desktop_store_http_error(error) from error
    payload = {
        "schema_version": 1,
        "releases": [
            _published_release_summary(release)
            for release in releases
            if release.get("published") is True
        ][:limit],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    etag = '"sha256:' + hashlib.sha256(encoded).hexdigest() + '"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(
        content=json.dumps(payload, ensure_ascii=False),
        media_type="application/json",
        headers={"ETag": etag},
    )


@app.get("/api/desktop-updates/feed/v1/releases/{version}")
async def get_desktop_release_feed_manifest(
    version: str,
    authorization: str | None = Header(default=None),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
):
    _require_feed_access(authorization)
    normalized = _normalize_version(version)
    try:
        release = await _desktop_release_store().read_release(normalized)
    except DesktopReleaseStoreError as error:
        raise _desktop_store_http_error(error) from error
    if release.get("published") is not True:
        raise HTTPException(status_code=404, detail=tr("api.desktop.release_missing"))
    payload = await _published_release_manifest(release)
    revision = str(payload["release"]["revision"])
    etag = '"' + revision + '"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(
        content=json.dumps(payload, ensure_ascii=False),
        media_type="application/json",
        headers={"ETag": etag},
    )


@app.get("/api/desktop-updates/assets/{version}/{filename}")
async def download_desktop_release_asset(
    version: str,
    filename: str,
    authorization: str | None = Header(default=None),
):
    normalized = _normalize_version(version)
    try:
        release = await _desktop_release_store().read_release(normalized)
    except DesktopReleaseStoreError as error:
        raise _desktop_store_http_error(error) from error
    if release.get("published") is not True:
        if authorization is None:
            raise HTTPException(status_code=404, detail=tr("api.desktop.update_asset_missing"))
        _require_admin(authorization)

    path = _asset_path(normalized, filename)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=tr("api.desktop.update_asset_missing"))
    return FileResponse(path)


@app.get("/api/desktop-updates/{target}/{arch}/{current_version}")
async def check_desktop_update(
    target: str,
    arch: str,
    current_version: str,
    request: Request,
):
    try:
        latest = await _desktop_release_store().read_latest()
    except DesktopReleaseStoreError as error:
        raise _desktop_store_http_error(error) from error
    if latest is None:
        return Response(status_code=204)
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
        try:
            release = await _desktop_release_store().read_release(latest_version)
        except DesktopReleaseStoreError:
            release = {}
        release_asset = (release.get("platforms") or {}).get(platform)
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
        raise HTTPException(status_code=410, detail=tr("api.desktop.legacy_bridge_removed"))
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
