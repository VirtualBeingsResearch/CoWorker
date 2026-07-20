import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from coworker.api import admin
from coworker.core.config import Config, apply_admin_config_file, ensure_admin_token
from coworker.core.types import Message
from coworker.memory.short_term import ShortTermMemory
from coworker.skills.loader import SkillLoader


class _Identity:
    name = "Luna"


def _client(tmp_path, *, providers_file: str = ""):
    config = Config.model_validate({
        "admin": {"token": "secret", "config_file": str(tmp_path / "admin_config.json")},
        "llm": {"openai_api_key": "sk-original", "providers_file": providers_file},
        "agent": {"logs_dir": str(tmp_path / "logs")},
    })
    agent = SimpleNamespace(_identity=_Identity(), request_restart=lambda: None)
    brain = SimpleNamespace(
        active_provider=object(),
        current_provider_name="openai",
        current_model="gpt-5.2",
        set_max_tokens=lambda value: None,
        list_providers=lambda: [],
        upsert_provider=AsyncMock(),
    )
    admin.setup_admin(
        agent=agent,
        brain=brain,
        config=config,
        alarm_manager=None,
        skill_loader=None,
        palace_loader=None,
        mode_loader=None,
    )
    app = FastAPI()
    app.include_router(admin.router)
    return TestClient(app), config


def test_admin_requires_bearer_token(tmp_path):
    client, _ = _client(tmp_path)
    assert client.post("/api/admin/session/verify").status_code == 401
    assert client.post(
        "/api/admin/session/verify", headers={"Authorization": "Bearer wrong"}
    ).status_code == 403
    response = client.post(
        "/api/admin/session/verify", headers={"Authorization": "Bearer secret"}
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Luna"


def test_config_response_masks_secrets_and_blank_form_does_not_clear_them(tmp_path):
    client, config = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}
    response = client.get("/api/admin/config", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["config"]["llm"]["openai_api_key"] == ""
    assert body["secret_status"]["llm.openai_api_key"] == {
        "configured": True,
        "last4": "inal",
    }

    llm_form = body["config"]["llm"]
    llm_form["max_tokens"] = 4096
    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={"changes": {"llm": llm_form}, "secrets": {}},
    )
    assert response.status_code == 200
    assert "llm.max_tokens" in response.json()["applied_now"]
    assert response.json()["requires_restart"] == []
    saved = json.loads((tmp_path / "admin_config.json").read_text(encoding="utf-8"))
    assert saved["llm"]["max_tokens"] == 4096
    assert "openai_api_key" not in saved["llm"]
    assert config.llm.openai_api_key == "sk-original"


def test_config_response_separates_external_and_managed_providers(tmp_path):
    providers_file = tmp_path / "providers.json"
    providers_file.write_text(json.dumps([{
        "name": "external-zhipu",
        "type": "zhipu",
        "api_key": "zk-external",
    }]), encoding="utf-8")
    client, _ = _client(tmp_path, providers_file=str(providers_file))
    headers = {"Authorization": "Bearer secret"}

    response = client.get("/api/admin/config", headers=headers)
    body = response.json()
    assert body["config"]["llm"]["managed_providers"] == []
    assert [provider["name"] for provider in body["effective_providers"]] == [
        "openai", "external-zhipu",
    ]
    assert all(provider["api_key"] == "" for provider in body["effective_providers"])
    assert all(provider["managed"] is False for provider in body["effective_providers"])

    llm_form = body["config"]["llm"]
    llm_form["max_tokens"] = 4096
    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={"changes": {"llm": llm_form}, "secrets": {}},
    )
    assert response.status_code == 200
    saved = json.loads((tmp_path / "admin_config.json").read_text(encoding="utf-8"))
    assert saved["llm"]["managed_providers"] == []
    assert "external-zhipu" not in json.dumps(saved)
    assert "zk-external" not in json.dumps(saved)


def test_config_patch_rebuilds_only_changed_managed_provider(tmp_path, monkeypatch):
    client, _ = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}
    built: list[str] = []

    def fake_build_provider(type_, api_key, *, base_url=None, name=None, default_model=None):
        built.append(str(name or type_))
        return SimpleNamespace(provider_name=name or type_)

    monkeypatch.setattr("coworker.brain.factory.build_provider", fake_build_provider)
    providers = [
        {"name": "admin-a", "type": "openai", "api_key": "", "base_url": "", "default_model": None},
        {"name": "admin-b", "type": "zhipu", "api_key": "", "base_url": "", "default_model": None},
    ]

    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={
            "changes": {"llm": {"managed_providers": providers}},
            "secrets": {
                "llm.managed_providers.0.api_key": "sk-a",
                "llm.managed_providers.1.api_key": "sk-b",
            },
        },
    )
    assert response.status_code == 200
    assert built == ["admin-a", "admin-b"]

    providers[0]["base_url"] = "https://new.example/v1"
    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={"changes": {"llm": {"managed_providers": providers}}, "secrets": {}},
    )
    assert response.status_code == 200
    assert built == ["admin-a", "admin-b", "admin-a"]
    saved = json.loads((tmp_path / "admin_config.json").read_text(encoding="utf-8"))
    assert [provider["api_key"] for provider in saved["llm"]["managed_providers"]] == [
        "sk-a", "sk-b",
    ]


def test_config_patch_reports_hot_and_restart_fields(tmp_path):
    client, _ = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}

    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={"changes": {"agent": {"idle_sleep_seconds": 12}}, "secrets": {}},
    )
    assert response.status_code == 200
    assert response.json()["applied_now"] == ["agent.idle_sleep_seconds"]
    assert response.json()["requires_restart"] == []
    assert response.json()["pending_restart"] is False

    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={"changes": {"api": {"port": 8123}}, "secrets": {}},
    )
    assert response.status_code == 200
    assert response.json()["applied_now"] == []
    assert response.json()["requires_restart"] == ["api.port"]
    assert response.json()["pending_restart"] is True

    # The form shows the saved desired value while the running Config remains unchanged.
    assert client.get("/api/admin/config", headers=headers).json()["config"]["api"]["port"] == 8123


def test_admin_overlay_has_higher_priority_than_base_config(tmp_path):
    path = tmp_path / "admin_config.json"
    path.write_text(json.dumps({"agent": {"idle_sleep_seconds": 7}}), encoding="utf-8")
    config = Config.model_validate({
        "admin": {"config_file": str(path)},
        "agent": {"idle_sleep_seconds": 30},
    })
    loaded = apply_admin_config_file(config)
    assert loaded.agent.idle_sleep_seconds == 7
    assert loaded.admin.config_file == str(path)


def test_first_run_admin_token_is_generated_and_preserves_overrides(tmp_path):
    path = tmp_path / "admin_config.json"
    path.write_text(json.dumps({"agent": {"tick": False}}), encoding="utf-8")
    config = Config.model_validate({
        "admin": {"token": "", "config_file": str(path)},
        "desktop_updates": {"admin_token": ""},
    })

    token = ensure_admin_token(config)

    assert token
    assert config.admin.token == token
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["admin"]["token"] == token
    assert saved["agent"]["tick"] is False
    assert ensure_admin_token(config) is None


def test_bootstrap_persists_first_provider_and_requests_restart(tmp_path):
    client, config = _client(tmp_path)
    admin._brain.active_provider = None
    admin._agent._identity._dir = tmp_path / "identity"
    admin._agent._identity.load = lambda: None
    headers = {"Authorization": "Bearer secret"}

    status = client.get("/api/admin/bootstrap", headers=headers)
    assert status.status_code == 200
    assert status.json()["required"] is True
    assert {item["type"] for item in status.json()["providers"]} >= {"openai", "deepseek"}

    response = client.post(
        "/api/admin/bootstrap",
        headers=headers,
        json={
            "provider_type": "openai",
            "model": "gpt-5.2",
            "api_key": "sk-first-run",
            "base_url": "https://example.test/v1",
            "coworker_name": "Nova",
        },
    )

    assert response.status_code == 202
    saved = json.loads((tmp_path / "admin_config.json").read_text(encoding="utf-8"))
    assert saved["llm"]["default_provider"] == "openai"
    assert saved["llm"]["default_model"] == "gpt-5.2"
    assert saved["llm"]["managed_providers"][0]["api_key"] == "sk-first-run"
    assert saved["memory"]["mem0_llm_provider"] == "openai"
    assert "agent" not in saved
    assert (tmp_path / "identity" / "name.txt").read_text(encoding="utf-8") == "Nova"
    assert config.admin.token == "secret"


def test_bootstrap_rejects_unknown_model_and_completed_installation(tmp_path):
    client, _ = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}
    payload = {
        "provider_type": "openai",
        "model": "not-a-model",
        "api_key": "sk-test",
    }
    assert client.post("/api/admin/bootstrap", headers=headers, json=payload).status_code == 409

    admin._brain.active_provider = None
    assert client.post("/api/admin/bootstrap", headers=headers, json=payload).status_code == 422


def test_overview_uses_short_term_configured_token_capacity(tmp_path):
    client, config = _client(tmp_path)
    short_term = ShortTermMemory(max_tokens=12_345)
    agent = SimpleNamespace(
        _identity=_Identity(),
        _task_store=SimpleNamespace(list=lambda: []),
        _bubble_store=SimpleNamespace(list_active=lambda: []),
        _long_term=SimpleNamespace(count=AsyncMock(return_value=3)),
        _short_term=short_term,
        state=SimpleNamespace(is_running=True, is_sleeping=False, cycle_count=8),
    )
    brain = SimpleNamespace(current_provider_name="deepseek", current_model="deepseek-chat")
    admin.setup_admin(
        agent=agent,
        brain=brain,
        config=config,
        alarm_manager=SimpleNamespace(list=lambda: []),
        skill_loader=None,
        palace_loader=None,
        mode_loader=None,
    )

    response = client.get(
        "/api/admin/overview", headers={"Authorization": "Bearer secret"}
    )
    assert response.status_code == 200
    assert response.json()["memory"]["max_tokens"] == 12_345


def test_bubble_history_survives_restart_and_preserves_raw_values(tmp_path):
    client, config = _client(tmp_path)
    bubble_dir = Path(config.agent.logs_dir) / "bubbles"
    bubble_dir.mkdir(parents=True)
    path = bubble_dir / "bbl_260716120000.jsonl"
    entries = [
        {"type": "message_in", "content": "最多执行 4 轮", "ts": "2026-07-16T12:00:00"},
        {
            "type": "tool_call",
            "name": "demo",
            "arguments": {"api_key": "secret"},
            "ts": "2026-07-16T12:00:01",
        },
        {
            "type": "llm_response",
            "usage": {"input_tokens": 12, "output_tokens": 3, "cached_tokens": 4},
            "ts": "2026-07-16T12:00:01",
        },
        {
            "__meta__": True,
            "id": "bbl_260716120000",
            "goal": "核对发布",
                "status": "done",
                "cycles_used": 1,
                "elapsed_seconds": 2,
                "participant_id": "wecom:alice",
                "conversation_id": "conv-frontend",
                "handoff_transparency": True,
                "resume_count": 1,
                "ts": "2026-07-16T12:00:02",
            },
    ]
    path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries),
        encoding="utf-8",
    )
    admin._agent._bubble_store = SimpleNamespace(list_active=lambda: [], _history=[])
    headers = {"Authorization": "Bearer secret"}

    response = client.get("/api/admin/bubbles", headers=headers)
    assert response.status_code == 200
    record = response.json()["bubbles"][0]
    assert record["goal"] == "核对发布"
    assert record["max_cycles"] == 4
    assert record["participant_id"] == "wecom:alice"
    assert record["conversation_id"] == "conv-frontend"
    assert record["handoff_transparency"] is True
    assert record["resume_count"] == 1
    assert response.json()["total"] == 1
    assert response.json()["has_more"] is False

    response = client.get("/api/admin/bubbles?limit=1&offset=1", headers=headers)
    assert response.json()["bubbles"] == []

    response = client.get("/api/admin/bubbles/bbl_260716120000/history", headers=headers)
    assert response.status_code == 200
    assert response.json()["events"][1]["arguments"]["api_key"] == "secret"
    assert response.json()["events"][2]["usage"] == {
        "input_tokens": 12,
        "output_tokens": 3,
        "cached_tokens": 4,
    }
    path.write_text(
        path.read_text(encoding="utf-8")
        + '\n{"type":"thinking_start","cycle":2,"ts":"2026-07-16T12:00:03"}',
        encoding="utf-8",
    )
    response = client.get("/api/admin/bubbles/bbl_260716120000/history", headers=headers)
    assert len(response.json()["events"]) == 5

    subconscious_dir = Path(config.agent.logs_dir) / "subconscious" / "bubbles"
    subconscious_dir.mkdir(parents=True)
    subconscious_path = subconscious_dir / "bbl_260716120000_audit.jsonl"
    subconscious_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    response = client.get("/api/admin/subconscious", headers=headers)
    assert response.status_code == 200
    assert response.json()["bubbles"][0]["mode"] == "audit"
    response = client.get(
        "/api/admin/subconscious/bbl_260716120000_audit/history", headers=headers
    )
    assert response.status_code == 200
    assert len(response.json()["events"]) == 5

    snapshot = SimpleNamespace(
        id="bbl_260716120001",
        status="running",
        goal="尚未落盘",
        result="",
        error="",
        created_at=datetime(2026, 7, 16, 12, 0, 1),
    )
    admin._agent._bubble_store.get = lambda bubble_id: (
        snapshot if bubble_id == snapshot.id else None
    )
    response = client.get(f"/api/admin/bubbles/{snapshot.id}/history", headers=headers)
    assert response.status_code == 200
    assert response.json()["events"][0]["type"] == "bubble_snapshot"


def test_admin_can_add_and_delete_pinned_context(tmp_path):
    client, config = _client(tmp_path)
    short_term = ShortTermMemory(max_tokens=1_000)
    agent = SimpleNamespace(
        _identity=_Identity(),
        _short_term=short_term,
        state=SimpleNamespace(last_main_response_usage=None),
    )
    admin.setup_admin(
        agent=agent,
        brain=SimpleNamespace(current_provider_name="openai", current_model="gpt-5.2"),
        config=config,
        alarm_manager=None,
        skill_loader=None,
        palace_loader=None,
        mode_loader=None,
    )
    headers = {"Authorization": "Bearer secret"}

    created = client.post(
        "/api/admin/memory/pinned",
        headers=headers,
        json={"label": "项目约定", "content": "保持接口向后兼容"},
    )

    assert created.status_code == 201
    pin_id = created.json()["pin_id"]
    assert [(item.label, item.content) for item in short_term.pinned_items] == [
        ("项目约定", "保持接口向后兼容")
    ]
    assert client.delete(f"/api/admin/memory/pinned/{pin_id}", headers=headers).status_code == 200
    assert short_term.pinned_items == []


def test_short_term_memory_falls_back_to_estimate_without_latest_usage(tmp_path):
    client, config = _client(tmp_path)
    short_term = ShortTermMemory(max_tokens=1_000)
    short_term.primary.append(Message(role="user", content="estimate me"))
    agent = SimpleNamespace(
        _identity=_Identity(),
        _short_term=short_term,
        state=SimpleNamespace(last_main_response_usage=None),
    )
    brain = SimpleNamespace(
        active_provider=None,
        current_provider_name="openai",
        current_model="gpt-5.2",
    )
    admin.setup_admin(
        agent=agent, brain=brain, config=config, alarm_manager=None,
        skill_loader=None, palace_loader=None, mode_loader=None,
    )

    body = client.get(
        "/api/admin/memory/short-term",
        headers={"Authorization": "Bearer secret"},
    ).json()

    assert body["token_watermark"]["source"] == "estimated"
    assert body["token_watermark"]["tokens"] > 0
    assert (
        body["token_watermark"]["tokens"]
        == body["token_watermark"]["estimated_short_term_tokens"]
    )


def test_short_term_memory_returns_wecom_structured_text_without_attachment_bytes(tmp_path):
    client, config = _client(tmp_path)
    short_term = ShortTermMemory(max_tokens=1_000)
    short_term.primary.append(
        Message(
            role="user",
            content=[
                {"type": "text", "text": "用户输入正文"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "secret-image-bytes",
                    },
                    "_filename": "example.png",
                },
            ],
            source="wecom",
        )
    )
    agent = SimpleNamespace(
        _identity=_Identity(),
        _short_term=short_term,
        state=SimpleNamespace(last_main_response_usage=None),
    )
    brain = SimpleNamespace(
        active_provider=None,
        current_provider_name="openai",
        current_model="gpt-5.2",
    )
    admin.setup_admin(
        agent=agent,
        brain=brain,
        config=config,
        alarm_manager=None,
        skill_loader=None,
        palace_loader=None,
        mode_loader=None,
    )

    response = client.get(
        "/api/admin/memory/short-term",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    message = response.json()["messages"][0]
    assert message["role"] == "user"
    assert message["source"] == "wecom"
    assert message["content"] == [
        {"type": "text", "text": "用户输入正文"},
        {"type": "image"},
    ]
    assert "secret-image-bytes" not in response.text


def test_content_registry_includes_parsed_metadata(tmp_path):
    client, config = _client(tmp_path)
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "release-notes"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Release Notes\ndescription: 整理版本变更\nversion: 2.1.0\n---\n\n# Steps\n",
        encoding="utf-8",
    )
    admin.setup_admin(
        agent=SimpleNamespace(_identity=_Identity()),
        brain=SimpleNamespace(),
        config=config,
        alarm_manager=None,
        skill_loader=SkillLoader(str(skills_dir)),
        palace_loader=None,
        mode_loader=None,
    )

    response = client.get(
        "/api/admin/content/skills",
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["id"] == "release-notes"
    assert item["name"] == "Release Notes"
    assert item["summary"] == "整理版本变更"
    assert item["valid"] is True
    assert item["metadata"] == {"version": "2.1.0"}
    assert item["size_bytes"] > 0
    assert item["files"][0]["path"] == "SKILL.md"
    assert item["files"][0]["primary"] is True


def test_content_folder_text_files_can_be_managed_safely(tmp_path):
    client, config = _client(tmp_path)
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "browser"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: browser\ndescription: 浏览器检查\n---\n\n# Browser\n",
        encoding="utf-8",
    )
    (skill_dir / "scripts" / "check.py").write_text("print('old')\n", encoding="utf-8")
    admin.setup_admin(
        agent=SimpleNamespace(_identity=_Identity()), brain=SimpleNamespace(), config=config,
        alarm_manager=None, skill_loader=SkillLoader(str(skills_dir)), palace_loader=None,
        mode_loader=None,
    )
    headers = {"Authorization": "Bearer secret"}

    response = client.get("/api/admin/content/skills/browser/files", headers=headers)
    assert [item["path"] for item in response.json()["files"]] == [
        "SKILL.md", "scripts/check.py",
    ]
    response = client.get(
        "/api/admin/content/skills/browser/files/scripts/check.py", headers=headers,
    )
    assert response.json()["content"] == "print('old')\n"

    response = client.put(
        "/api/admin/content/skills/browser/files/scripts/check.py",
        headers=headers,
        json={"content": "print('new')\n"},
    )
    assert response.status_code == 200
    assert (skill_dir / "scripts" / "check.py").read_text(encoding="utf-8") == "print('new')\n"
    assert client.get(
        "/api/admin/content/skills/browser/files/../outside.py", headers=headers,
    ).status_code in (400, 404)
    assert client.delete(
        "/api/admin/content/skills/browser/files/SKILL.md", headers=headers,
    ).status_code == 409
    assert client.delete(
        "/api/admin/content/skills/browser", headers=headers,
    ).status_code == 200
    assert not skill_dir.exists()


def test_admin_interaction_history_pages_every_shard_and_loads_detail(tmp_path):
    client, config = _client(tmp_path)
    logs_dir = Path(config.agent.logs_dir)
    logs_dir.mkdir(parents=True)
    archived = [
        {"type": "message_in", "seq": 0, "ts": "2026-07-01T09:00:00", "content": "出生"},
        {"type": "system_prompt", "seq": 1, "ts": "2026-07-01T09:01:00", "content": "系统提示"},
        {"type": "tool_call", "seq": 2, "ts": "2026-07-01T09:02:00", "name": "read_file"},
    ]
    active = [
        {"type": "tool_result", "seq": 3, "ts": "2026-07-01T09:03:00", "name": "read_file", "content": "ok"},
        {"type": "llm_response", "seq": 4, "ts": "2026-07-01T09:04:00", "content": "现在"},
    ]
    (logs_dir / "interactions-000001.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in archived) + "\n",
        encoding="utf-8",
    )
    (logs_dir / "interactions.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in active) + "\n",
        encoding="utf-8",
    )
    headers = {"Authorization": "Bearer secret"}

    first = client.get("/api/admin/interactions?limit=2", headers=headers)
    assert first.status_code == 200
    assert [item["seq"] for item in first.json()["events"]] == [4, 3]
    assert first.json()["next_cursor"]
    assert first.json()["sequence"] == {"first": 0, "latest": 4, "total": 5}

    second = client.get(
        "/api/admin/interactions?limit=2&cursor=" + first.json()["next_cursor"],
        headers=headers,
    )
    assert [item["seq"] for item in second.json()["events"]] == [2, 1]
    assert second.json()["events"][1]["type"] == "system_prompt"

    third = client.get(
        "/api/admin/interactions?limit=2&cursor=" + second.json()["next_cursor"],
        headers=headers,
    )
    assert [item["seq"] for item in third.json()["events"]] == [0]
    assert third.json()["has_more"] is False

    detail = client.get("/api/admin/interactions/1", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["entry"]["content"] == "系统提示"


def test_admin_interaction_history_rejects_invalid_cursor(tmp_path):
    client, _ = _client(tmp_path)
    response = client.get(
        "/api/admin/interactions?cursor=not-a-cursor",
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 400


def test_admin_interaction_history_can_jump_to_a_sequence_range(tmp_path):
    client, config = _client(tmp_path)
    logs_dir = Path(config.agent.logs_dir)
    logs_dir.mkdir(parents=True)
    (logs_dir / "interactions-000001.jsonl").write_text(
        "\n".join(json.dumps({"seq": seq, "ts": f"2026-07-01T09:0{seq}:00", "type": "message_in"}) for seq in range(3)) + "\n",
        encoding="utf-8",
    )
    (logs_dir / "interactions.jsonl").write_text(
        "\n".join(json.dumps({"seq": seq, "ts": f"2026-07-01T09:0{seq}:00", "type": "tool_result"}) for seq in range(3, 5)) + "\n",
        encoding="utf-8",
    )
    headers = {"Authorization": "Bearer secret"}

    response = client.get(
        "/api/admin/interactions?limit=100&seq_start=1&seq_end=3",
        headers=headers,
    )

    assert response.status_code == 200
    assert [item["seq"] for item in response.json()["events"]] == [3, 2, 1]
    assert response.json()["has_more"] is False
    assert client.get(
        "/api/admin/interactions?seq_start=4&seq_end=3",
        headers=headers,
    ).status_code == 400


def test_legacy_admin_logs_endpoint_is_not_available(tmp_path):
    client, _ = _client(tmp_path)
    response = client.get("/api/admin/logs", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 404
