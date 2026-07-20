from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from coworker.api import app as api_app
from coworker.api.routes import setup as setup_routes
from coworker.api.ws import ConnectionPool, serialize_outbound_message
from coworker.brain.brain import Brain
from coworker.core.config import APIConfig
from coworker.core.types import AgentState, CommunicateRequest
from coworker.memory.short_term import ShortTermMemory
from coworker.tools.communicate_tool import CommunicateTool
from tests.conftest import MockProvider


@pytest.fixture
def client():
    # reset module-level state before each test
    import coworker.api.routes as routes_mod
    routes_mod._inbox = None
    routes_mod._agent = None
    routes_mod._brain = None
    routes_mod._usage_stats = None
    routes_mod._model_config_path = Path("data/model_runtime_config.json")
    routes_mod._profile_readme_last_reminded_at = None
    routes_mod._communication_token = ""
    routes_mod._development_mode = False
    routes_mod._seen_desktop_message_ids.clear()
    api_app._desktop_updates_effective = None
    api_app._desktop_updates_admin_token = ""
    api_app._inbox = None
    api_app._communicate = None
    api_app._collector = None
    api_app._pool = ConnectionPool()
    api_app._shutting_down = False
    return TestClient(api_app.app)


def test_api_defaults_bind_locally_and_require_desktop_authentication():
    config = APIConfig(_env_file=None)

    assert config.host == "127.0.0.1"
    assert config.development_mode is False
    assert "*" not in config.cors_origins


def test_admin_ui_is_bundled(client):
    response = client.get("/admin")

    assert response.status_code == 200
    assert '<div id="root"></div>' in response.text


class TestPostMessages:
    def test_returns_503_when_not_ready(self, client):
        resp = client.post("/messages", json={"sender_id": "alice", "content": "hi"})
        assert resp.status_code == 503

    def test_queues_event_when_ready(self, client):
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        mock_agent = MagicMock()
        mock_brain = MagicMock()
        setup_routes(mock_inbox, mock_agent, mock_brain)

        resp = client.post("/messages", json={"sender_id": "alice", "content": "hello"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "queued"
        assert body["sender_id"] == "alice"
        mock_inbox.push.assert_called_once()


    @pytest.mark.parametrize(
        "sender_id", ["codex:codex-local", "local:codex-local", "codex-bridge:old"]
    )
    def test_legacy_desktop_sender_is_rejected(self, client, sender_id):
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        mock_agent = MagicMock()
        mock_brain = MagicMock()
        setup_routes(mock_inbox, mock_agent, mock_brain)

        resp = client.post(
            "/messages",
            json={
                "sender_id": sender_id,
                "conversation_id": "thr_1",
                "content": "done",
            },
        )

        assert resp.status_code == 422
        mock_inbox.push.assert_not_awaited()

    def test_attachment_filename_is_sanitized(self, client, tmp_path):
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        mock_agent = MagicMock()
        mock_brain = MagicMock()
        setup_routes(mock_inbox, mock_agent, mock_brain, inbox_dir=str(tmp_path / "inbox"))

        resp = client.post(
            "/messages",
            json={
                "sender_id": "alice",
                "content": "",
                "attachments": [
                    {
                        "filename": "..\\..\\evil:name.txt",
                        "media_type": "text/plain",
                        "data": base64.b64encode(b"hello").decode("ascii"),
                    }
                ],
            },
        )

        assert resp.status_code == 200
        event = mock_inbox.push.call_args.args[0]
        attachment = event.attachments[0]
        saved_path = Path(attachment.saved_path).resolve()
        attachments_dir = (tmp_path / "attachments").resolve()
        assert saved_path.parent == attachments_dir
        assert len(saved_path.name.split("_", 1)[0]) == 12
        assert attachment.filename == "evil-name.txt"
        assert saved_path.read_bytes() == b"hello"

    def test_desktop_thread_envelope_extracts_attachment_instead_of_exposing_base64(
        self, client, tmp_path
    ):
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        setup_routes(
            mock_inbox,
            MagicMock(),
            MagicMock(),
            inbox_dir=str(tmp_path / "inbox"),
            development_mode=True,
        )
        encoded = base64.b64encode(b"image bytes").decode("ascii")
        envelope = {
            "protocol_version": 1,
            "message_id": "desktop-message-1",
            "conversation_id": "session-1",
            "created_at": "2026-07-13T00:00:00Z",
            "type": "desktop.thread.event",
            "payload": {
                "actor_id": "claude",
                "author_kind": "local",
                "message": "请查看附件",
                "attachments": [{
                    "filename": "screen.png",
                    "media_type": "image/png",
                    "data": encoded,
                }],
            },
        }

        request = {
            "sender_id": "coworker-desktop:desk:claude:cw:participant",
            **envelope,
        }
        response = client.post("/messages", json=request)

        assert response.status_code == 200
        assert response.json() == {
            "message_id": "desktop-message-1",
            "accepted": True,
            "duplicate": False,
        }
        event = mock_inbox.push.call_args.args[0]
        assert event.content == "请查看附件"
        assert event.conversation_id == "session-1"
        assert encoded not in event.content
        assert len(event.attachments) == 1
        assert event.attachments[0].filename == "screen.png"
        assert event.attachments[0].data is None
        assert Path(event.attachments[0].saved_path).read_bytes() == b"image bytes"

    def test_duplicate_desktop_message_id_is_acked_without_requeueing(self, client):
        # bridge 出站是"至少一次"：HTTP POST 成功但响应丢失时它会用同一 message_id 重发。
        # coworker 必须按 message_id 幂等去重，第二条只 ack 不再 push。
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        setup_routes(mock_inbox, MagicMock(), MagicMock(), development_mode=True)
        message = {
            "sender_id": "coworker-desktop:desk:claude:cw:participant",
            "protocol_version": 1,
            "message_id": "desktop-message-dup",
            "created_at": "2026-07-13T00:00:00Z",
            "type": "desktop.thread.event",
            "payload": {"actor_id": "claude", "message": "hello"},
        }

        first = client.post("/messages", json=message)
        second = client.post("/messages", json=message)

        assert first.status_code == 200
        assert first.json() == {
            "message_id": "desktop-message-dup",
            "accepted": True,
            "duplicate": False,
        }
        assert second.status_code == 200
        assert second.json() == {
            "message_id": "desktop-message-dup",
            "accepted": True,
            "duplicate": True,
        }
        mock_inbox.push.assert_awaited_once()

        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        setup_routes(mock_inbox, MagicMock(), MagicMock(), development_mode=True)
        request = {
            "sender_id": "coworker-desktop:desk:claude:cw:participant",
            "protocol_version": 1,
            "message_id": "desktop-snapshot-1",
            "created_at": "2026-07-13T00:00:00Z",
            "type": "desktop.actor.snapshot",
            "payload": {"desktop_id": "desk", "actor_id": "claude"},
        }

        response = client.post("/messages", json=request)

        assert response.status_code == 200
        event = mock_inbox.push.call_args.args[0]
        envelope = json.loads(event.content)
        assert envelope == {key: value for key, value in request.items() if key != "sender_id"}

    def test_desktop_message_requires_matching_bearer_by_default(self, client):
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        setup_routes(
            mock_inbox,
            MagicMock(),
            MagicMock(),
            communication_token="secret",
        )
        message = {
            "sender_id": "coworker-desktop:desk:claude:cw:participant",
            "type": "desktop.thread.event",
            "payload": {"actor_id": "claude", "message": "hello"},
            "message_id": "desktop-message-2",
            "protocol_version": 1,
            "created_at": "2026-07-13T00:00:00Z",
        }

        rejected = client.post("/messages", json=message)
        accepted = client.post(
            "/messages",
            json=message,
            headers={"Authorization": "Bearer secret"},
        )

        assert rejected.status_code == 401
        assert accepted.status_code == 200
        mock_inbox.push.assert_awaited_once()


class TestSSE:
    def test_format_sse_single_line(self):
        assert api_app._format_sse("hi") == "data: hi\n\n"

    def test_format_sse_multiline(self):
        # 多行 message：每行加 data: 前缀，空行收尾（EventSource 会用 \n 重组）
        assert api_app._format_sse("a\nb") == "data: a\ndata: b\n\n"

    def test_format_sse_structured_payload(self):
        assert (
            api_app._format_sse(
                CommunicateRequest(
                    participant_id="alice",
                    message="hi",
                    conversation_id="thr_1",
                    extra={
                        "bubble": {
                            "id": "bbl_frontend",
                            "kind": "handoff",
                            "phase": "start",
                            "resumed": False,
                        }
                    },
                )
            )
            == (
                'data: {"participant_id": "alice", "message": "hi", '
                '"conversation_id": "thr_1", "extra": {"bubble": '
                '{"id": "bbl_frontend", "kind": "handoff", "phase": "start", '
                '"resumed": false}}}\n\n'
            )
        )

    def test_ws_serialization_encodes_attachments(self, tmp_path):
        file_path = tmp_path / "note.txt"
        file_path.write_text("hello", encoding="utf-8")

        payload = json.loads(
            serialize_outbound_message(
                CommunicateRequest(
                    participant_id="alice",
                    message="hi",
                    attachments=[{"path": str(file_path)}],
                )
            )
        )

        assert payload["attachments"] == [
            {
                "filename": "note.txt",
                "media_type": "text/plain",
                "data": base64.b64encode(b"hello").decode("ascii"),
            }
        ]

    def test_sse_route_registered(self):
        # 不实跑流：无限生成器与测试客户端的关闭/取消语义相冲会挂死，
        # 故只断言路由已注册（防误删/拼错）。实际流式投递由 curl 手动验证（见计划）。
        paths = {getattr(r, "path", None) for r in api_app.app.routes}
        assert "/sse/{participant_id}" in paths

    @pytest.mark.asyncio
    async def test_connection_pool_sends_structured_payload_as_json(self):
        sent: list[str] = []

        class FakeWebSocket:
            async def send_text(self, message: str) -> None:
                sent.append(message)

        pool = ConnectionPool()
        pool._connections["alice"] = FakeWebSocket()
        pool._outboxes["alice"] = asyncio.Queue()

        await pool.send(
            "alice",
            CommunicateRequest(
                participant_id="alice",
                message="hi",
                conversation_id="thr_1",
            ),
        )

        assert sent == [
            '{"participant_id": "alice", "message": "hi", "conversation_id": "thr_1"}'
        ]

    def test_runtime_log_history_days_is_bounded(self, client):
        resp = client.get("/logs/stream?history_days=31")
        assert resp.status_code == 422

    def test_runtime_log_history_lines_is_bounded(self, client):
        resp = client.get("/logs/stream?history_lines=20001")
        assert resp.status_code == 422


class TestUnregisterWsGuard:
    def test_duplicate_registration_is_rejected_and_first_queue_kept(self):
        comm = CommunicateTool("unused")
        first_q: asyncio.Queue = asyncio.Queue()
        second_q: asyncio.Queue = asyncio.Queue()
        assert comm.register_ws("alice", first_q) is True
        assert comm.register_ws("alice", second_q) is False
        assert comm._ws_connections.get("alice") is first_q
        # 被拒绝的 queue 不应删掉先到的连接
        comm.unregister_ws("alice", second_q)
        assert comm._ws_connections.get("alice") is first_q
        # 用匹配的 queue 注销才生效
        comm.unregister_ws("alice", first_q)
        assert "alice" not in comm._ws_connections

    def test_no_queue_arg_removes_unconditionally(self):
        comm = CommunicateTool("unused")
        comm.register_ws("bob", asyncio.Queue())
        comm.unregister_ws("bob")  # 向后兼容：不传 queue 直接删
        assert "bob" not in comm._ws_connections

    def test_connection_listener_only_fires_on_real_connection_changes(self):
        comm = CommunicateTool("unused")
        events: list[list[str]] = []
        comm.add_connection_listener(lambda: events.append(comm.list_connected()))

        first_q: asyncio.Queue = asyncio.Queue()
        second_q: asyncio.Queue = asyncio.Queue()

        assert comm.register_ws("alice", first_q) is True
        assert events == [["alice"]]

        assert comm.register_ws("alice", second_q) is False
        comm.unregister_ws("alice", second_q)
        assert events == [["alice"]]

        comm.unregister_ws("alice", first_q)
        assert events == [["alice"], []]


class TestConnectionRejection:
    def test_desktop_websocket_requires_bearer_in_production(self, client, tmp_path):
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        api_app.setup_ws(mock_inbox, CommunicateTool(str(tmp_path / "outbox")))
        setup_routes(
            mock_inbox,
            MagicMock(),
            MagicMock(),
            communication_token="secret",
        )
        participant_id = "coworker-desktop:desk:claude:cw:participant"

        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(f"/ws/{participant_id}"):
                pass
        assert error.value.code == 1008

        with client.websocket_connect(
            f"/ws/{participant_id}",
            headers={"Authorization": "Bearer secret"},
        ):
            pass

    def test_websocket_json_message_uses_message_field(self, client, tmp_path):
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        comm = CommunicateTool(str(tmp_path))
        api_app.setup_ws(mock_inbox, comm)

        with client.websocket_connect("/ws/alice") as ws:
            ws.send_json({"message": "hi", "conversation_id": "thr_1"})

        event = mock_inbox.push.await_args.args[0]
        assert event.participant_id == "alice"
        assert event.content == "hi"
        assert event.conversation_id == "thr_1"
        assert event.source == "websocket"

    def test_websocket_duplicate_gets_rejection_message(self, client, tmp_path):
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        comm = CommunicateTool(str(tmp_path))
        api_app.setup_ws(mock_inbox, comm)

        with client.websocket_connect("/ws/alice"):
            with client.websocket_connect("/ws/alice") as duplicate:
                msg = duplicate.receive_text()
                assert "连接被拒绝" in msg
                assert "先到先得" in msg
                with pytest.raises(WebSocketDisconnect) as exc:
                    duplicate.receive_text()
                assert exc.value.code == 1008

            assert "alice" in comm._ws_connections

        assert "alice" not in comm._ws_connections

    def test_sse_duplicate_gets_rejection_event(self, client, tmp_path):
        comm = CommunicateTool(str(tmp_path))
        assert comm.register_ws("alice", asyncio.Queue()) is True
        api_app.setup_ws(MagicMock(), comm)

        resp = client.get("/sse/alice")

        assert resp.status_code == 200
        assert resp.headers["x-connection-rejected"] == "duplicate-participant"
        assert "data: 连接被拒绝" in resp.text
        assert "先到先得" in resp.text

    def test_legacy_codex_bridge_connections_are_rejected(self, client):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws/codex-bridge:old") as socket:
                socket.receive_text()
        assert exc.value.code == 1008

        response = client.get("/sse/codex-bridge:old")
        assert response.status_code == 410


class TestCommunicateRegistrationAPI:
    def test_legacy_codex_bridge_registration_is_rejected(self, client, tmp_path):
        comm = CommunicateTool(str(tmp_path / "outbox"))
        api_app.setup_ws(MagicMock(), comm)

        response = client.post(
            "/api/communicate/register",
            json={
                "kind": "codex-bridge",
                "client_id": "codex-local:cw_default",
                "metadata": {"protocol_versions": [1]},
            },
        )

        assert response.status_code == 422

    def test_desktop_registration_negotiates_protocol_or_rejects(self, client, tmp_path):
        comm = CommunicateTool(str(tmp_path / "outbox"))
        api_app.setup_ws(MagicMock(), comm)
        setup_routes(MagicMock(), MagicMock(), MagicMock(), development_mode=True)

        incompatible = client.post(
            "/api/communicate/register",
            json={
                "kind": "coworker-desktop",
                "client_id": "desk:claude:cw_default",
                "metadata": {"protocol_versions": [99]},
            },
        )
        assert incompatible.status_code == 422

        compatible = client.post(
            "/api/communicate/register",
            json={
                "kind": "coworker-desktop",
                "client_id": "desk:claude:cw_default",
                "metadata": {"protocol_versions": [1]},
            },
        )
        assert compatible.status_code == 200
        assert compatible.json()["negotiated_protocol_version"] == 1

    def test_register_lists_and_deletes_inactive_registration(self, client, tmp_path):
        comm = CommunicateTool(str(tmp_path / "outbox"))
        api_app.setup_ws(MagicMock(), comm)
        setup_routes(MagicMock(), MagicMock(), MagicMock(), development_mode=True)

        resp = client.post(
            "/api/communicate/register",
            json={
                "kind": "coworker-desktop",
                "client_id": "desk:local:cw_default",
                "display_name": "Local Desktop",
                "metadata": {"protocol_versions": [1]},
            },
        )

        assert resp.status_code == 200
        registration = resp.json()
        assert registration["participant_id"].startswith("coworker-desktop:d:local:")
        assert len(registration["participant_id"]) == 33
        assert registration["active"] is False

        list_resp = client.get("/api/communicate/register")
        assert list_resp.status_code == 200
        assert (
            list_resp.json()["registrations"][0]["registration_id"]
            == registration["registration_id"]
        )

        delete_resp = client.delete(f"/api/communicate/register/{registration['registration_id']}")
        assert delete_resp.status_code == 200
        assert delete_resp.json()["deleted"]["registration_id"] == registration["registration_id"]
        assert client.get("/api/communicate/register").json()["registrations"] == []

    def test_register_reuses_inactive_registration(self, client, tmp_path):
        comm = CommunicateTool(str(tmp_path / "outbox"))
        api_app.setup_ws(MagicMock(), comm)
        setup_routes(MagicMock(), MagicMock(), MagicMock(), development_mode=True)

        first = client.post(
            "/api/communicate/register",
            json={
                "kind": "coworker-desktop",
                "client_id": "desk:local:cw_default",
                "metadata": {"protocol_versions": [1]},
            },
        ).json()
        second = client.post(
            "/api/communicate/register",
            json={
                "kind": "coworker-desktop",
                "client_id": "desk:local:cw_default",
                "metadata": {"protocol_versions": [1]},
            },
        ).json()

        assert second["registration_id"] == first["registration_id"]
        assert second["participant_id"] == first["participant_id"]

    def test_active_registration_gets_new_id_and_cannot_be_deleted(self, client, tmp_path):
        comm = CommunicateTool(str(tmp_path / "outbox"))
        api_app.setup_ws(MagicMock(), comm)
        setup_routes(MagicMock(), MagicMock(), MagicMock(), development_mode=True)
        first = client.post(
            "/api/communicate/register",
            json={
                "kind": "coworker-desktop",
                "client_id": "desk:local:cw_default",
                "metadata": {"protocol_versions": [1]},
            },
        ).json()
        assert comm.register_ws(first["participant_id"], asyncio.Queue()) is True

        second = client.post(
            "/api/communicate/register",
            json={
                "kind": "coworker-desktop",
                "client_id": "desk:local:cw_default",
                "metadata": {"protocol_versions": [1]},
            },
        ).json()

        assert second["registration_id"] != first["registration_id"]
        assert second["participant_id"] != first["participant_id"]
        list_resp = client.get("/api/communicate/register").json()["registrations"]
        assert [item["active"] for item in list_resp] == [True, False]
        delete_resp = client.delete(f"/api/communicate/register/{first['registration_id']}")
        assert delete_resp.status_code == 409


class TestGetStatus:
    def test_returns_not_started_when_no_agent(self, client):
        import coworker.api.routes as routes_mod
        routes_mod._agent = None
        resp = client.get("/status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_started"

    def test_returns_agent_state(self, client):
        mock_agent = MagicMock()
        mock_agent.state = AgentState(
            is_running=True,
            is_sleeping=False,
            current_provider="anthropic",
            current_model="claude-sonnet-4-6",
            cycle_count=7,
        )
        mock_brain = MagicMock()
        mock_brain.list_providers.return_value = ["anthropic", "zhipu-userA"]
        mock_brain.model_config_snapshot.return_value = {
            "providers": ["anthropic", "zhipu-userA"],
            "active": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            "summary": {"provider": "", "model": "", "thinking": False},
            "fallbacks": [],
            "vision": {"provider": "", "model": "", "thinking": True, "enabled": False},
        }
        import coworker.api.routes as routes_mod
        routes_mod._agent = mock_agent
        routes_mod._brain = mock_brain

        resp = client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_running"] is True
        assert data["cycle_count"] == 7
        assert data["provider"] == "anthropic"
        assert data["model"] == "claude-sonnet-4-6"
        assert data["providers"] == ["anthropic", "zhipu-userA"]
        assert data["model_config"]["fallbacks"] == []

    def test_returns_usage_stats_when_available(self, client):
        mock_inbox = MagicMock()
        mock_agent = MagicMock()
        mock_agent.state = AgentState(
            is_running=True,
            current_provider="openai",
            current_model="gpt-4o",
        )
        mock_stats = MagicMock()
        mock_stats.snapshot.return_value = {
            "today": {"llm_calls": 1, "total_tokens": 12},
            "last_7_days": {"llm_calls": 1, "total_tokens": 12},
            "lifetime": {"llm_calls": 1, "total_tokens": 12},
        }
        mock_brain = MagicMock()
        mock_brain.list_providers.return_value = ["openai"]
        mock_brain.model_config_snapshot.return_value = {
            "providers": ["openai"],
            "active": {"provider": "openai", "model": "gpt-4o"},
            "summary": {"provider": "", "model": "", "thinking": False},
            "fallbacks": [],
            "vision": {"provider": "", "model": "", "thinking": True, "enabled": False},
        }
        setup_routes(mock_inbox, mock_agent, mock_brain, usage_stats=mock_stats)

        resp = client.get("/status")

        assert resp.status_code == 200
        assert resp.json()["usage_stats"]["today"]["total_tokens"] == 12
        mock_stats.snapshot.assert_called_once()


def _agent_with_profile(tmp_path, readme: str | None = None, days_old: int = 0):
    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()
    if readme is not None:
        path = identity_dir / "profile.md"
        path.write_text(readme, encoding="utf-8")
        if days_old:
            ts = (datetime.now() - timedelta(days=days_old)).timestamp()
            os.utime(path, (ts, ts))
    identity = MagicMock()
    identity._dir = identity_dir
    identity.name = "Luna"
    identity.is_initialized = True
    identity.personality = ""
    identity.goals = ""

    mock_agent = MagicMock()
    mock_agent._identity = identity
    mock_agent._short_term.log_store = None
    mock_agent._snapshot_path = tmp_path / "memory" / "short_term_snapshot.json"
    return mock_agent, identity_dir


class TestGetProfile:
    def test_returns_profile_readme_when_present(self, client, tmp_path):
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        mock_agent, _ = _agent_with_profile(tmp_path, readme="I am Luna.")
        setup_routes(mock_inbox, mock_agent, MagicMock())

        resp = client.get("/profile")

        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Luna"
        assert body["readme"] == "I am Luna."
        mock_inbox.push.assert_not_called()

    @pytest.mark.parametrize(("readme", "days_old"), [(None, 0), ("I am Luna.", 31)])
    def test_profile_readme_requests_generation_or_update_once(
        self, client, tmp_path, readme, days_old
    ):
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        mock_agent, _ = _agent_with_profile(tmp_path, readme=readme, days_old=days_old)
        setup_routes(mock_inbox, mock_agent, MagicMock())

        first = client.get("/profile")
        second = client.get("/profile")

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["readme"] == readme
        mock_inbox.push.assert_called_once()
        assert "profile.md" in mock_inbox.push.call_args.args[0].content


class TestSwitchModel:
    def test_returns_503_when_not_ready(self, client):
        resp = client.post("/switch_model", json={"provider": "openai", "model_id": "gpt-4o"})
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_switch_succeeds(self, client):
        from unittest.mock import AsyncMock
        mock_brain = MagicMock()
        mock_brain.switch_model = AsyncMock()
        mock_brain.current_model = "gpt-4o"
        import coworker.api.routes as routes_mod
        routes_mod._brain = mock_brain

        resp = client.post("/switch_model", json={"provider": "openai", "model_id": "gpt-4o"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "switched"
        assert resp.json()["model_id"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_switch_allows_default_model(self, client):
        from unittest.mock import AsyncMock
        mock_brain = MagicMock()
        mock_brain.switch_model = AsyncMock()
        mock_brain.current_model = "qwen-plus"
        import coworker.api.routes as routes_mod
        routes_mod._brain = mock_brain

        resp = client.post("/switch_model", json={"provider": "qwen"})
        assert resp.status_code == 200
        mock_brain.switch_model.assert_awaited_once_with("qwen", "")
        assert resp.json()["model_id"] == "qwen-plus"

    @pytest.mark.asyncio
    async def test_switch_returns_400_on_error(self, client):
        from unittest.mock import AsyncMock
        mock_brain = MagicMock()
        mock_brain.switch_model = AsyncMock(side_effect=ValueError("bad model"))
        import coworker.api.routes as routes_mod
        routes_mod._brain = mock_brain

        resp = client.post("/switch_model", json={"provider": "x", "model_id": "bad"})
        assert resp.status_code == 400
        assert "bad model" in resp.json()["detail"]


def _model_config_brain() -> Brain:
    brain = Brain("mock", "mock-model")
    brain.register_provider(MockProvider())
    return brain


class TestModelConfigAPI:
    def test_get_returns_503_when_not_ready(self, client):
        resp = client.get("/model_config")
        assert resp.status_code == 503

    def test_get_returns_snapshot(self, client, tmp_path):
        mock_inbox = MagicMock()
        mock_agent = MagicMock()
        brain = _model_config_brain()
        path = tmp_path / "model_runtime_config.json"
        setup_routes(mock_inbox, mock_agent, brain, model_config_path=path)

        resp = client.get("/model_config")

        assert resp.status_code == 200
        body = resp.json()
        assert body["providers"] == ["mock"]
        assert body["active"] == {"provider": "mock", "model": "mock-model"}
        assert body["vision"]["thinking"] is True
        assert body["persisted"] is False
        assert body["override_path"] == str(path)

    def test_patch_updates_brain_and_persists(self, client, tmp_path):
        mock_inbox = MagicMock()
        mock_agent = MagicMock()
        brain = _model_config_brain()
        path = tmp_path / "model_runtime_config.json"
        setup_routes(mock_inbox, mock_agent, brain, model_config_path=path)

        resp = client.patch(
            "/model_config",
            json={
                "summary": {"provider": "mock", "model": "summary-model", "thinking": True},
                "fallbacks": ["mock/mock-model"],
                "vision": {"provider": "mock", "model": "vision-model", "thinking": False},
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"] == {"provider": "mock", "model": "summary-model", "thinking": True}
        assert body["fallbacks"] == ["mock/mock-model"]
        assert body["vision"]["provider"] == "mock"
        assert body["vision"]["model"] == "vision-model"
        assert body["vision"]["thinking"] is False
        assert body["persisted"] is True
        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert persisted["summary"]["model"] == "summary-model"
        assert persisted["vision"]["model"] == "vision-model"
        assert persisted["vision"]["thinking"] is False
        assert brain.summary_model == "summary-model"
        assert brain.vision_model == "vision-model"
        assert brain.vision_thinking is False

    def test_patch_invalid_payload_returns_400_and_does_not_write(self, client, tmp_path):
        mock_inbox = MagicMock()
        mock_agent = MagicMock()
        brain = _model_config_brain()
        path = tmp_path / "model_runtime_config.json"
        setup_routes(mock_inbox, mock_agent, brain, model_config_path=path)

        resp = client.patch(
            "/model_config",
            json={"vision": {"provider": "mock", "model": ""}},
        )

        assert resp.status_code == 400
        assert "vision.provider" in resp.json()["detail"]
        assert not path.exists()
        assert brain.vision_provider_name == ""


class TestBackfillTree:
    def test_returns_503_when_not_ready(self, client):
        resp = client.post("/backfill_tree", json={})
        assert resp.status_code == 503

    def test_returns_400_when_no_log_store(self, client):
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        mock_agent = MagicMock()
        mock_agent._short_term.log_store = None
        setup_routes(mock_inbox, mock_agent, MagicMock())
        resp = client.post("/backfill_tree", json={})
        assert resp.status_code == 400

    def test_returns_started_when_ready(self, client):
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        mock_agent = MagicMock()
        mock_agent._short_term.log_store = object()  # truthy
        mock_agent._short_term.backfill_progress = {"running": False, "done": 0, "total": 0}
        mock_agent._short_term.backfill_tree_online = AsyncMock(return_value=3)
        mock_agent._short_term.tree.nodes = []
        setup_routes(mock_inbox, mock_agent, MagicMock())
        resp = client.post("/backfill_tree", json={"max_leaves": 8})
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"
        assert resp.json()["max_leaves"] == 8

    def test_returns_409_when_already_running(self, client):
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        mock_agent = MagicMock()
        mock_agent._short_term.log_store = object()
        mock_agent._short_term.backfill_progress = {"running": True, "done": 2, "total": 5}
        setup_routes(mock_inbox, mock_agent, MagicMock())
        resp = client.post("/backfill_tree", json={})
        assert resp.status_code == 409

    def test_get_progress(self, client):
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        mock_agent = MagicMock()
        mock_agent._short_term.backfill_progress = {"running": True, "done": 3, "total": 7}
        setup_routes(mock_inbox, mock_agent, MagicMock())
        resp = client.get("/backfill_tree")
        assert resp.status_code == 200
        assert resp.json() == {"running": True, "done": 3, "total": 7}

    def test_get_progress_not_started(self, client):
        resp = client.get("/backfill_tree")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_started"


def _make_backup(tmp_path, name="emergency_backup_20260609_101112.json", primary=None):
    if primary is None:
        primary = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
    p = tmp_path / name
    p.write_text(json.dumps({"primary": primary}), encoding="utf-8")
    return p


def _agent_with_backups(tmp_path, stm=None):
    mock_agent = MagicMock()
    mock_agent._snapshot_path = tmp_path / "short_term_snapshot.json"
    if stm is not None:
        mock_agent._short_term = stm
    return mock_agent


class TestListBackups:
    def test_returns_503_when_not_ready(self, client):
        resp = client.get("/backups")
        assert resp.status_code == 503

    def test_lists_backups(self, client, tmp_path):
        _make_backup(tmp_path)
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        setup_routes(mock_inbox, _agent_with_backups(tmp_path), MagicMock())
        resp = client.get("/backups")
        assert resp.status_code == 200
        backups = resp.json()["backups"]
        assert len(backups) == 1
        assert backups[0]["filename"] == "emergency_backup_20260609_101112.json"
        assert backups[0]["message_count"] == 2
        assert backups[0]["timestamp"] is not None

    def test_empty_when_no_backups(self, client, tmp_path):
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        setup_routes(mock_inbox, _agent_with_backups(tmp_path), MagicMock())
        resp = client.get("/backups")
        assert resp.status_code == 200
        assert resp.json()["backups"] == []


class TestRestoreBackup:
    def test_returns_503_when_not_ready(self, client):
        resp = client.post("/backups/restore", json={"filename": "x", "mode": "full"})
        assert resp.status_code == 503

    def test_rejects_path_traversal(self, client, tmp_path):
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        setup_routes(mock_inbox, _agent_with_backups(tmp_path), MagicMock())
        resp = client.post(
            "/backups/restore",
            json={"filename": "../emergency_backup_x.json", "mode": "full"},
        )
        assert resp.status_code == 400

    def test_returns_404_when_missing(self, client, tmp_path):
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        setup_routes(mock_inbox, _agent_with_backups(tmp_path), MagicMock())
        resp = client.post(
            "/backups/restore",
            json={"filename": "emergency_backup_20990101_000000.json", "mode": "full"},
        )
        assert resp.status_code == 404

    def test_rejects_empty_backup(self, client, tmp_path):
        _make_backup(tmp_path, primary=[])
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        stm = ShortTermMemory()
        setup_routes(mock_inbox, _agent_with_backups(tmp_path, stm), MagicMock())
        resp = client.post(
            "/backups/restore",
            json={"filename": "emergency_backup_20260609_101112.json", "mode": "full"},
        )
        assert resp.status_code == 400

    def test_full_restore_replaces_primary(self, client, tmp_path):
        _make_backup(tmp_path)
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        stm = ShortTermMemory()
        setup_routes(mock_inbox, _agent_with_backups(tmp_path, stm), MagicMock())
        resp = client.post(
            "/backups/restore",
            json={"filename": "emergency_backup_20260609_101112.json", "mode": "full"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "restored"
        assert body["mode"] == "full"
        assert body["message_count"] == 2
        assert len(stm.primary) == 2
        assert stm.primary[0].content == "a"
        mock_inbox.push.assert_called_once()

    def test_summarize_restore_injects_and_keeps_primary(self, client, tmp_path):
        _make_backup(tmp_path)
        mock_inbox = MagicMock()
        mock_inbox.push = AsyncMock()
        stm = ShortTermMemory()
        mock_brain = MagicMock()
        mock_brain.summarize = AsyncMock(return_value='{"summary": "digest"}')
        setup_routes(mock_inbox, _agent_with_backups(tmp_path, stm), mock_brain)
        resp = client.post(
            "/backups/restore",
            json={"filename": "emergency_backup_20260609_101112.json", "mode": "summarize"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "summarize"
        assert body["summary"] == "digest"
        # summarize 模式不改 primary
        assert stm.primary == []
        mock_inbox.push.assert_called_once()


def _desktop_update_env(monkeypatch, tmp_path):
    monkeypatch.setattr(
        api_app,
        "_desktop_updates_config",
        lambda: api_app.DesktopUpdatesConfig(
            dir=str(tmp_path / "desktop_updates"),
            admin_token="secret-token",
        ),
    )
    return {"Authorization": "Bearer secret-token"}


class TestDesktopUpdatesAPI:
    def test_admin_requires_token(self, client, monkeypatch, tmp_path):
        _desktop_update_env(monkeypatch, tmp_path)
        resp = client.post("/api/desktop-updates/releases", json={"version": "0.2.0"})
        assert resp.status_code == 401

    def test_admin_rejects_wrong_token(self, client, monkeypatch, tmp_path):
        _desktop_update_env(monkeypatch, tmp_path)
        resp = client.post(
            "/api/desktop-updates/releases",
            json={"version": "0.2.0"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 403

    def test_management_token_uses_effective_desktop_updates_config(self, client, tmp_path):
        updates_dir = tmp_path / "managed-desktop-updates"
        api_app.setup_desktop_updates(
            api_app.DesktopUpdatesConfig(dir=str(updates_dir), admin_token=""),
            "management-token",
        )

        response = client.post(
            "/api/desktop-updates/releases",
            json={"version": "0.2.0"},
            headers={"Authorization": "Bearer management-token"},
        )

        assert response.status_code == 200
        assert (updates_dir / "releases" / "0.2.0" / "release.json").is_file()

    def test_separate_management_and_legacy_tokens_are_both_accepted(self, client, tmp_path):
        api_app.setup_desktop_updates(
            api_app.DesktopUpdatesConfig(
                dir=str(tmp_path / "desktop-updates"),
                admin_token="legacy-token",
            ),
            "management-token",
        )

        for token in ("legacy-token", "management-token"):
            response = client.get(
                "/api/desktop-updates/releases",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200

    def test_create_upload_publish_and_check_update(self, client, monkeypatch, tmp_path):
        headers = _desktop_update_env(monkeypatch, tmp_path)
        create = client.post(
            "/api/desktop-updates/releases",
            json={"version": "0.2.0", "notes": "desktop update"},
            headers=headers,
        )
        assert create.status_code == 200
        assert create.json()["version"] == "0.2.0"

        upload = client.post(
            "/api/desktop-updates/releases/0.2.0/assets",
            headers=headers,
            data={
                "platform": "windows-x86_64",
                "signature": "sig-content",
                "kind": "updater",
            },
            files={"file": ("Coworker.exe", b"binary", "application/octet-stream")},
        )
        assert upload.status_code == 200
        assert upload.json()["platforms"]["windows-x86_64"]["signature"] == "sig-content"

        installer = client.post(
            "/api/desktop-updates/releases/0.2.0/assets",
            headers=headers,
            data={
                "platform": "darwin-x86_64",
                "kind": "installer",
            },
            files={"file": ("Coworker.dmg", b"installer", "application/octet-stream")},
        )
        assert installer.status_code == 200
        assert installer.json()["installers"]["darwin-x86_64"]["file"] == "Coworker.dmg"

        publish = client.post(
            "/api/desktop-updates/releases/0.2.0/publish",
            json={"platforms": ["windows-x86_64"]},
            headers=headers,
        )
        assert publish.status_code == 200
        body = publish.json()
        assert body["version"] == "0.2.0"
        assert body["platforms"]["windows-x86_64"]["signature"] == "sig-content"
        latest_file = tmp_path / "desktop_updates" / "latest.json"
        latest_data = json.loads(latest_file.read_text(encoding="utf-8"))
        assert latest_data["platforms"]["windows-x86_64"]["file"] == "Coworker.exe"
        assert "url" not in latest_data["platforms"]["windows-x86_64"]

        check = client.get("/api/desktop-updates/windows/x86_64/0.1.0")
        assert check.status_code == 200
        assert check.json()["version"] == "0.2.0"
        assert check.json()["signature"] == "sig-content"

        moved = client.get(
            "/api/desktop-updates/windows/x86_64/0.1.0",
            headers={"host": "updates.example.test"},
        )
        assert moved.json()["url"].startswith(
            "http://updates.example.test/api/desktop-updates/assets/"
        )

        current = client.get("/api/desktop-updates/windows/x86_64/0.2.0")
        assert current.status_code == 204

        asset_url = body["platforms"]["windows-x86_64"]["url"]
        asset_path = asset_url.split("testserver", 1)[1]
        asset = client.get(asset_path)
        assert asset.status_code == 200
        assert asset.content == b"binary"

        installer_asset = client.get("/api/desktop-updates/assets/0.2.0/Coworker.dmg")
        assert installer_asset.status_code == 200
        assert installer_asset.content == b"installer"

        release_list = client.get("/api/desktop-updates/releases", headers=headers)
        assert release_list.status_code == 200
        assert release_list.json()["releases"][0]["installers"] == ["darwin-x86_64"]

    def test_publish_pushes_one_update_check_per_eligible_online_desktop(
        self, client, monkeypatch, tmp_path
    ):
        headers = _desktop_update_env(monkeypatch, tmp_path)
        communicate = CommunicateTool(str(tmp_path / "outbox"))
        api_app._communicate = communicate

        def register(client_id, desktop_id, version, capabilities):
            registration = communicate.register_participant(
                kind="coworker-desktop",
                client_id=client_id,
                metadata={
                    "desktop_id": desktop_id,
                    "desktop_version": version,
                    "capabilities": capabilities,
                },
            )
            queue = asyncio.Queue()
            communicate.register_ws(registration["participant_id"], queue)
            return queue

        old_local = register("desk-old:local:cw", "desk-old", "0.1.0", ["desktop_update_push"])
        old_codex = register("desk-old:codex:cw", "desk-old", "0.1.0", ["desktop_update_push"])
        current = register(
            "desk-current:local:cw", "desk-current", "0.2.0", ["desktop_update_push"]
        )
        legacy = register("desk-legacy:local:cw", "desk-legacy", "0.1.0", [])
        communicate.register_participant(
            kind="coworker-desktop",
            client_id="desk-offline:local:cw",
            metadata={
                "desktop_id": "desk-offline",
                "desktop_version": "0.1.0",
                "capabilities": ["desktop_update_push"],
            },
        )

        client.post("/api/desktop-updates/releases", json={"version": "0.2.0"}, headers=headers)
        client.post(
            "/api/desktop-updates/releases/0.2.0/assets",
            headers=headers,
            data={"platform": "windows-x86_64", "signature": "sig", "kind": "updater"},
            files={"file": ("Coworker.exe", b"binary", "application/octet-stream")},
        )
        response = client.post("/api/desktop-updates/releases/0.2.0/publish", headers=headers)

        assert response.status_code == 200
        assert response.json()["push"] == {"eligible": 1, "enqueued": 1}
        request = old_local.get_nowait()
        assert request.extra["operation"] == "check_desktop_update"
        assert request.extra["published_version"] == "0.2.0"
        assert request.extra["request_id"]
        assert old_codex.empty()
        assert current.empty()
        assert legacy.empty()

    def test_publish_single_platform_preserves_existing_latest_platforms(
        self, client, monkeypatch, tmp_path
    ):
        headers = _desktop_update_env(monkeypatch, tmp_path)
        client.post("/api/desktop-updates/releases", json={"version": "0.2.0"}, headers=headers)
        client.post(
            "/api/desktop-updates/releases/0.2.0/assets",
            headers=headers,
            data={"platform": "windows-x86_64", "signature": "win-sig", "kind": "updater"},
            files={"file": ("Coworker.exe", b"win-binary", "application/octet-stream")},
        )
        client.post(
            "/api/desktop-updates/releases/0.2.0/assets",
            headers=headers,
            data={"platform": "darwin-aarch64", "signature": "mac-sig", "kind": "updater"},
            files={"file": ("Coworker.app.tar.gz", b"mac-binary", "application/octet-stream")},
        )

        initial_publish = client.post(
            "/api/desktop-updates/releases/0.2.0/publish",
            json={"platforms": ["windows-x86_64", "darwin-aarch64"]},
            headers=headers,
        )
        assert initial_publish.status_code == 200

        client.post(
            "/api/desktop-updates/releases/0.2.0/assets",
            headers=headers,
            data={"platform": "windows-x86_64", "signature": "win-sig-2", "kind": "updater"},
            files={"file": ("Coworker-2.exe", b"win-binary-2", "application/octet-stream")},
        )
        partial_publish = client.post(
            "/api/desktop-updates/releases/0.2.0/publish",
            json={"platforms": ["windows-x86_64"]},
            headers=headers,
        )

        assert partial_publish.status_code == 200
        platforms = partial_publish.json()["platforms"]
        assert sorted(platforms) == ["darwin-aarch64", "windows-x86_64"]
        assert platforms["windows-x86_64"]["signature"] == "win-sig-2"
        assert platforms["darwin-aarch64"]["signature"] == "mac-sig"

        mac_check = client.get("/api/desktop-updates/darwin/aarch64/0.1.0")
        assert mac_check.status_code == 200
        assert mac_check.json()["signature"] == "mac-sig"

    def test_mac_updater_upload_qualifies_generic_tarball_names(
        self, client, monkeypatch, tmp_path
    ):
        headers = _desktop_update_env(monkeypatch, tmp_path)
        client.post("/api/desktop-updates/releases", json={"version": "0.2.0"}, headers=headers)

        arm_upload = client.post(
            "/api/desktop-updates/releases/0.2.0/assets",
            headers=headers,
            data={"platform": "darwin-aarch64", "signature": "arm-sig", "kind": "updater"},
            files={"file": ("app.tar.gz", b"arm-binary", "application/gzip")},
        )
        x64_upload = client.post(
            "/api/desktop-updates/releases/0.2.0/assets",
            headers=headers,
            data={"platform": "darwin-x86_64", "signature": "x64-sig", "kind": "updater"},
            files={"file": ("app.tar.gz", b"x64-binary", "application/gzip")},
        )

        assert arm_upload.status_code == 200
        assert x64_upload.status_code == 200
        platforms = x64_upload.json()["platforms"]
        assert platforms["darwin-aarch64"]["file"] == "darwin-aarch64-app.tar.gz"
        assert platforms["darwin-x86_64"]["file"] == "darwin-x86_64-app.tar.gz"

        publish = client.post(
            "/api/desktop-updates/releases/0.2.0/publish",
            json={"platforms": ["darwin-aarch64", "darwin-x86_64"]},
            headers=headers,
        )

        assert publish.status_code == 200
        latest = publish.json()["platforms"]
        arm_url = latest["darwin-aarch64"]["url"]
        x64_url = latest["darwin-x86_64"]["url"]
        assert arm_url.endswith("/darwin-aarch64-app.tar.gz")
        assert x64_url.endswith("/darwin-x86_64-app.tar.gz")
        assert arm_url != x64_url

        arm_asset = client.get(arm_url.split("testserver", 1)[1])
        x64_asset = client.get(x64_url.split("testserver", 1)[1])
        assert arm_asset.status_code == 200
        assert x64_asset.status_code == 200
        assert arm_asset.content == b"arm-binary"
        assert x64_asset.content == b"x64-binary"

    def test_upload_rejects_missing_signature(self, client, monkeypatch, tmp_path):
        headers = _desktop_update_env(monkeypatch, tmp_path)
        client.post("/api/desktop-updates/releases", json={"version": "0.2.0"}, headers=headers)
        upload = client.post(
            "/api/desktop-updates/releases/0.2.0/assets",
            headers=headers,
            data={"platform": "linux-x86_64", "signature": "", "kind": "updater"},
            files={"file": ("Coworker.AppImage", b"binary", "application/octet-stream")},
        )
        assert upload.status_code == 422

    def test_rejects_invalid_platform_and_path_traversal(self, client, monkeypatch, tmp_path):
        headers = _desktop_update_env(monkeypatch, tmp_path)
        client.post("/api/desktop-updates/releases", json={"version": "0.2.0"}, headers=headers)
        bad_platform = client.post(
            "/api/desktop-updates/releases/0.2.0/assets",
            headers=headers,
            data={"platform": "windows-amd64", "signature": "sig"},
            files={"file": ("Coworker.exe", b"binary", "application/octet-stream")},
        )
        assert bad_platform.status_code == 422

        traversal = client.get("/api/desktop-updates/assets/0.2.0/..%2Fsecret.txt")
        assert traversal.status_code in {400, 404, 422}
