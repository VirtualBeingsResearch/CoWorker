import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coworker.api.admin import router_module as admin
from coworker.application import _print_setup_admin_token
from coworker.channels.access import ChannelAccessController
from coworker.channels.module import ChannelModuleRegistry
from coworker.channels.traffic import ChannelTrafficStore
from coworker.channels.wecom import WeComChannel, WeComModule, WeComSettings
from coworker.core.config import (
    Config,
    apply_admin_config_file,
    effective_admin_token,
    effective_communication_token,
    ensure_admin_token,
    normalize_admin_overrides_file,
)
from coworker.core.types import Message
from coworker.desktop_updates import SyncStatus
from coworker.i18n import locale_context
from coworker.identity.identity import Identity
from coworker.memory.short_term import ShortTermMemory
from coworker.persona import PersonaCard, PersonStore
from coworker.skills.loader import SkillLoader


class _Identity:
    name = "Luna"


def _client(
    tmp_path,
    *,
    providers_file: str = "",
    api: dict | None = None,
    desktop_updates: dict | None = None,
    desktop_update_sync=None,
    alarm_manager=None,
    wecom: dict | None = None,
    weixin: dict | None = None,
    channel_access: dict | None = None,
    channel_modules=None,
    relay_client=None,
    persona: bool = False,
    usage_stats=None,
    long_term=None,
):
    config = Config.model_validate(
        {
            "admin": {"token": "secret", "config_file": str(tmp_path / "admin_config.json")},
            "api": api or {},
            "llm": {"openai_api_key": "sk-original", "providers_file": providers_file},
            "memory": {"db_path": str(tmp_path / "memory")},
            "agent": {"logs_dir": str(tmp_path / "logs")},
            "desktop_updates": desktop_updates or {},
            "wecom": wecom or {},
            "weixin": weixin or {},
            "channel_access": channel_access or {},
        }
    )
    agent = SimpleNamespace(
        _identity=_Identity(),
        request_restart=lambda reason="normal": None,
        resume_from_rest=MagicMock(return_value=True),
        current_system_prompt=MagicMock(return_value="[IDENTITY]\nMy name is Luna.\n"),
        refresh_system_prompt=MagicMock(),
    )
    _brain_snapshot = {
        "providers": ["openai"],
        "active": {"provider": "openai", "model": "gpt-5.2"},
        "summary": {"provider": "", "model": "", "thinking": False},
        "fallbacks": [],
        "vision": {"provider": "", "model": "", "thinking": True, "enabled": False},
    }
    brain = SimpleNamespace(
        active_provider=object(),
        current_provider_name="openai",
        current_model="gpt-5.2",
        set_max_tokens=lambda value: None,
        list_providers=lambda: [],
        upsert_provider=AsyncMock(),
        model_config_snapshot=lambda: _brain_snapshot,
        update_model_config=AsyncMock(return_value=_brain_snapshot),
    )
    person_store = PersonStore(tmp_path / "persons.json") if persona else None
    persona_cards = PersonaCard() if persona else None
    admin.setup_admin(
        agent=agent,
        brain=brain,
        config=config,
        alarm_manager=alarm_manager,
        skill_loader=None,
        palace_loader=None,
        mode_loader=None,
        desktop_update_sync=desktop_update_sync,
        relay_client=relay_client,
        person_store=person_store,
        persona_cards=persona_cards,
        usage_stats=usage_stats,
        long_term=long_term,
    )
    admin.setup_channel_admin(channel_modules or ChannelModuleRegistry())
    app = FastAPI()
    app.include_router(admin.router)
    return TestClient(app), config


def test_relay_status_does_not_return_token_until_explicitly_requested(tmp_path):
    class FakeRelayClient:
        def snapshot(self, *, include_token: bool = False):
            result = {"status": "connected", "instance_id": "cw_abcdefgh"}
            if include_token:
                result["communication_token"] = "desktop-secret"
            return result

    client, _ = _client(tmp_path, relay_client=FakeRelayClient())
    headers = {"Authorization": "Bearer secret"}

    status = client.get("/api/admin/relay", headers=headers)
    token = client.get("/api/admin/relay/token", headers=headers)

    assert status.status_code == 200
    assert "communication_token" not in status.json()
    assert token.json() == {"communication_token": "desktop-secret"}


def test_relay_token_rotation_uses_built_in_client(tmp_path):
    class FakeRelayClient:
        def __init__(self):
            self.rotate_token = AsyncMock(
                return_value={"status": "connecting", "instance_id": "cw_abcdefgh"}
            )

        def snapshot(self, *, include_token: bool = False):
            return {"status": "connected", "instance_id": "cw_abcdefgh"}

    relay = FakeRelayClient()
    client, _ = _client(tmp_path, relay_client=relay)
    response = client.post(
        "/api/admin/relay/rotate-token",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert response.json()["instance_id"] == "cw_abcdefgh"
    relay.rotate_token.assert_awaited_once()


def test_admin_alarm_accepts_browser_utc_timestamp(tmp_path):
    alarm_manager = SimpleNamespace(set=AsyncMock(), list=MagicMock(return_value=[]))
    client, _ = _client(tmp_path, alarm_manager=alarm_manager)
    trigger_at = datetime.now(UTC) + timedelta(minutes=5)

    response = client.post(
        "/api/admin/alarms",
        headers={"Authorization": "Bearer secret"},
        json={
            "trigger_at": trigger_at.isoformat().replace("+00:00", "Z"),
            "message": "browser alarm",
        },
    )

    assert response.status_code == 200
    alarm_manager.set.assert_awaited_once()
    scheduled_at = alarm_manager.set.await_args.args[1]
    assert scheduled_at.tzinfo is not None


def test_setup_admin_refreshes_config_service_for_each_runtime(tmp_path):
    _client(tmp_path / "first")
    first_service = admin._require_admin_config_service()

    _client(tmp_path / "second")

    assert admin._require_admin_config_service() is not first_service


def test_admin_requires_bearer_token(tmp_path):
    client, _ = _client(tmp_path)
    assert client.post("/api/admin/session/verify").status_code == 401
    assert (
        client.post(
            "/api/admin/session/verify", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 403
    )
    response = client.post("/api/admin/session/verify", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert response.json()["name"] == "Luna"
    assert response.json()["confirmation_name"] == "Luna"


def test_admin_usage_requires_admin_and_returns_detailed_report(tmp_path):
    report = {
        "today": {"llm_calls": 2, "total_tokens": 34},
        "last_7_days": {"llm_calls": 5, "total_tokens": 89},
        "last_30_days": {"llm_calls": 7, "total_tokens": 120},
        "lifetime": {"llm_calls": 8, "total_tokens": 144},
        "daily": [{"date": "2026-06-29", "total_tokens": 34}],
    }
    usage_stats = SimpleNamespace(report=MagicMock(return_value=report))
    client, _ = _client(tmp_path, usage_stats=usage_stats)

    assert client.get("/api/admin/usage").status_code == 401
    assert (
        client.get(
            "/api/admin/usage", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 403
    )
    usage_stats.report.assert_not_called()

    response = client.get(
        "/api/admin/usage", headers={"Authorization": "Bearer secret"}
    )

    assert response.status_code == 200
    assert response.json() == report
    usage_stats.report.assert_called_once_with()


def test_admin_usage_serializes_schema_timestamps_without_guessing_field_names(tmp_path):
    report = {
        "generated_at": "2026-08-13T10:30:00",
        "today": {"last_memory_compression_at": "2026-08-13T09:15:00"},
        "daily": [{"date": "2026-08-13", "total_tokens": 34}],
        "metadata": {"created_at": "2026-08-13T08:00:00"},
        "today_intraday": [
            {
                "start_time": "2026-08-13T09:00:00",
                "end_time": "2026-08-13T09:59:59.999999",
                "total_tokens": 34,
            }
        ],
    }
    usage_stats = SimpleNamespace(report=MagicMock(return_value=report))
    client, _ = _client(tmp_path, usage_stats=usage_stats)

    response = client.get(
        "/api/admin/usage", headers={"Authorization": "Bearer secret"}
    )

    assert response.status_code == 200
    payload = response.json()
    timestamps = (
        payload["generated_at"],
        payload["today"]["last_memory_compression_at"],
        payload["today_intraday"][0]["start_time"],
        payload["today_intraday"][0]["end_time"],
    )
    assert all(datetime.fromisoformat(value).utcoffset() is not None for value in timestamps)
    assert payload["daily"][0]["date"] == "2026-08-13"
    assert payload["metadata"]["created_at"] == "2026-08-13T08:00:00"


def test_admin_usage_returns_a_requested_date_range(tmp_path):
    report = {
        "selected_range": {
            "start_date": "2026-06-20",
            "end_date": "2026-06-22",
            "stats": {"total_tokens": 42},
            "daily": [],
        }
    }
    usage_stats = SimpleNamespace(report=MagicMock(return_value=report))
    client, _ = _client(tmp_path, usage_stats=usage_stats)

    response = client.get(
        "/api/admin/usage?start_date=2026-06-20&end_date=2026-06-22",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert response.json() == report
    usage_stats.report.assert_called_once_with(
        start_date=date(2026, 6, 20),
        end_date=date(2026, 6, 22),
    )


def test_admin_usage_treats_one_requested_date_as_a_single_day(tmp_path):
    usage_stats = SimpleNamespace(report=MagicMock(return_value={"selected_range": {}}))
    client, _ = _client(tmp_path, usage_stats=usage_stats)

    response = client.get(
        "/api/admin/usage?start_date=2026-06-20",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    usage_stats.report.assert_called_once_with(
        start_date=date(2026, 6, 20),
        end_date=date(2026, 6, 20),
    )


def test_admin_usage_rejects_a_reversed_date_range(tmp_path):
    usage_stats = SimpleNamespace(report=MagicMock())
    client, _ = _client(tmp_path, usage_stats=usage_stats)

    with locale_context("zh-CN"):
        response = client.get(
            "/api/admin/usage?start_date=2026-06-22&end_date=2026-06-20",
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "起始日期不能晚于结束日期"
    usage_stats.report.assert_not_called()


def test_admin_usage_collector_is_cleared_for_the_next_runtime(tmp_path):
    usage_stats = SimpleNamespace(
        report=MagicMock(
            return_value={
                "today": {},
                "last_7_days": {},
                "last_30_days": {},
                "lifetime": {},
                "daily": [],
            }
        )
    )
    first_client, _ = _client(tmp_path / "first", usage_stats=usage_stats)
    headers = {"Authorization": "Bearer secret"}
    assert first_client.get("/api/admin/usage", headers=headers).status_code == 200

    second_client, _ = _client(tmp_path / "second")
    with locale_context("zh-CN"):
        response = second_client.get("/api/admin/usage", headers=headers)

    assert response.status_code == 503
    assert response.json()["detail"] == "用量统计尚未就绪"


def test_admin_session_provides_stable_unnamed_confirmation(tmp_path):
    client, _ = _client(tmp_path)
    admin._agent._identity.name = ""
    headers = {"Authorization": "Bearer secret"}

    response = client.post(
        "/api/admin/session/verify",
        headers=headers,
    )

    assert response.json() == {
        "ok": True,
        "name": "",
        "confirmation_name": "Coworker",
    }
    rejected = client.post(
        "/api/admin/restart",
        headers=headers,
        json={"confirm_name": "未命名"},
    )
    accepted = client.post(
        "/api/admin/restart",
        headers=headers,
        json={"confirm_name": response.json()["confirmation_name"]},
    )
    assert rejected.status_code == 400
    assert accepted.status_code == 202


def test_admin_resume_wakes_rest_without_confirmation(tmp_path):
    client, _ = _client(tmp_path)

    assert client.post("/api/admin/resume").status_code == 401
    response = client.post(
        "/api/admin/resume",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert response.json() == {"resumed": True}
    admin._agent.resume_from_rest.assert_called_once_with()


def test_admin_error_detail_follows_runtime_locale(tmp_path):
    client, _ = _client(tmp_path)

    with locale_context("zh-CN"):
        chinese = client.post("/api/admin/session/verify")
    with locale_context("en"):
        english = client.post("/api/admin/session/verify")

    assert chinese.json()["detail"] == "缺少管理员令牌"
    assert english.json()["detail"] == "Administrator token is missing"


def test_identity_api_exposes_only_active_identity_fields(tmp_path):
    client, _ = _client(tmp_path)
    identity = Identity(str(tmp_path / "identity"))
    identity.update(
        {
            "name": "Luna",
            "personality": "curious",
            "current_location": "Paris",
        }
    )
    admin._agent._identity = identity
    headers = {"Authorization": "Bearer secret"}

    response = client.get("/api/admin/identity", headers=headers)

    assert response.json() == {
        "name": "Luna",
        "personality": "curious",
        "current_location": "Paris",
    }

    updated = client.put(
        "/api/admin/identity",
        headers=headers,
        json={"personality": "warm"},
    )

    assert updated.status_code == 200
    assert updated.json()["personality"] == "warm"
    admin._agent.refresh_system_prompt.assert_called_once_with()


def test_system_prompt_api_is_authenticated_read_only_and_uncached(tmp_path):
    client, _ = _client(tmp_path)

    assert client.get("/api/admin/system-prompt").status_code == 401

    response = client.get(
        "/api/admin/system-prompt",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "content": "[IDENTITY]\nMy name is Luna.\n",
        "characters": 28,
        "lines": 2,
    }
    admin._agent.current_system_prompt.assert_called_once_with()


def test_identity_api_rejects_all_retired_fields_together(tmp_path):
    client, _ = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}

    response = client.put(
        "/api/admin/identity",
        headers=headers,
        json={"goals": "ship", "life_story": "history"},
    )

    assert response.status_code == 422
    rejected_fields = {tuple(error["loc"]) for error in response.json()["detail"]}
    assert rejected_fields == {("body", "goals"), ("body", "life_story")}


def test_config_response_masks_secrets_and_blank_form_does_not_clear_them(tmp_path):
    client, config = _client(tmp_path, api={"communication_token": "desktop-secret"})
    headers = {"Authorization": "Bearer secret"}
    response = client.get("/api/admin/config", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["config"]["llm"]["openai_api_key"] == ""
    assert body["secret_status"]["llm.openai_api_key"] == {
        "configured": True,
        "last4": "inal",
    }
    assert body["config"]["api"]["communication_token"] == ""
    assert body["secret_status"]["api.communication_token"] == {
        "configured": True,
        "last4": "cret",
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
    assert config.api.communication_token == "desktop-secret"


def test_desktop_update_sync_config_status_and_trigger_are_safe(tmp_path):
    source_id = "11111111-1111-4111-8111-111111111111"
    sync_service = SimpleNamespace(
        status=AsyncMock(
            return_value=SyncStatus(
                enabled=True,
                ready=True,
                readiness="ready",
                outcome="idle",
                source={
                    "source_id": source_id,
                    "name": "GitHub upstream",
                    "provider": "github",
                    "endpoint": "https://api.example.test/api/v3",
                    "target": "acme/coworker",
                    "options": {"include_drafts": True},
                },
            )
        ),
        request_sync=AsyncMock(return_value={"run_id": "sync-1", "coalesced": False}),
    )
    client, _ = _client(
        tmp_path,
        desktop_updates={
            "sync_sources": [
                {
                    "id": source_id,
                    "name": "GitHub upstream",
                    "type": "github",
                    "api_base_url": "https://api.example.test/api/v3",
                    "repository": "acme/coworker",
                    "token": "github-secret-token",
                    "include_drafts": True,
                }
            ],
            "sync_active_source": source_id,
        },
        desktop_update_sync=sync_service,
    )
    headers = {"Authorization": "Bearer secret"}

    config_response = client.get("/api/admin/config", headers=headers)
    assert config_response.status_code == 200
    config_body = config_response.json()
    source = config_body["config"]["desktop_updates"]["sync_sources"][0]
    assert source["token"] == ""
    assert config_body["secret_status"][f"desktop_updates.sync_sources.{source_id}.token"] == {
        "configured": True,
        "last4": "oken",
    }

    status_response = client.get("/api/admin/desktop-updates/sync", headers=headers)
    assert status_response.status_code == 200
    assert status_response.json()["source"]["target"] == "acme/coworker"
    assert status_response.json()["token_configured"] is True
    assert "github-secret-token" not in status_response.text

    trigger_response = client.post("/api/admin/desktop-updates/sync", headers=headers)
    assert trigger_response.status_code == 202
    assert trigger_response.json() == {
        "accepted": True,
        "run_id": "sync-1",
        "coalesced": False,
    }
    sync_service.request_sync.assert_awaited_once_with("manual")
    audit = (tmp_path / "logs" / "admin_audit.jsonl").read_text(encoding="utf-8")
    assert "desktop_updates.sync.trigger" in audit
    assert "github-secret-token" not in audit


def test_desktop_update_sync_trigger_requires_enabled_config(tmp_path):
    client, _ = _client(tmp_path)
    response = client.post(
        "/api/admin/desktop-updates/sync",
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 409


def test_config_response_separates_external_and_managed_providers(tmp_path):
    providers_file = tmp_path / "providers.json"
    providers_file.write_text(
        json.dumps(
            [
                {
                    "name": "external-zhipu",
                    "type": "zhipu",
                    "api_key": "zk-external",
                }
            ]
        ),
        encoding="utf-8",
    )
    client, _ = _client(tmp_path, providers_file=str(providers_file))
    headers = {"Authorization": "Bearer secret"}

    response = client.get("/api/admin/config", headers=headers)
    body = response.json()
    assert body["config"]["llm"]["managed_providers"] == []
    assert [provider["name"] for provider in body["effective_providers"]] == [
        "openai",
        "external-zhipu",
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
    assert "managed_providers" not in saved["llm"]
    assert "external-zhipu" not in json.dumps(saved)
    assert "zk-external" not in json.dumps(saved)


def test_config_patch_rebuilds_only_changed_managed_provider(tmp_path, monkeypatch):
    client, _ = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}
    built: list[str] = []

    def fake_build_provider(
        type_,
        api_key,
        *,
        base_url=None,
        name=None,
        default_model=None,
        tool_use_models=None,
        model_capabilities=None,
    ):
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
        "sk-a",
        "sk-b",
    ]


def test_config_patch_hot_applies_managed_provider_model_capabilities(tmp_path, monkeypatch):
    client, _ = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}
    built_capabilities = []

    def fake_build_provider(type_, api_key, *, model_capabilities=None, **kwargs):
        built_capabilities.extend(model_capabilities or [])
        return SimpleNamespace(
            provider_name=kwargs.get("name") or type_,
            model_capabilities=model_capabilities or [],
        )

    monkeypatch.setattr("coworker.brain.factory.build_provider", fake_build_provider)
    declared = {
        "model": "gateway-omni",
        "tools": True,
        "vision": True,
        "video": False,
    }

    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={
            "changes": {
                "llm": {
                    "managed_providers": [
                        {
                            "name": "admin-custom",
                            "type": "openai",
                            "api_key": "",
                            "model_capabilities": [declared],
                        }
                    ]
                }
            },
            "secrets": {"llm.managed_providers.0.api_key": "sk-custom"},
        },
    )

    assert response.status_code == 200
    assert [capability.model_dump() for capability in built_capabilities] == [declared]
    hot_provider = admin._brain.upsert_provider.await_args.args[0]
    assert [capability.model_dump() for capability in hot_provider.model_capabilities] == [declared]
    saved = json.loads((tmp_path / "admin_config.json").read_text(encoding="utf-8"))
    assert saved["llm"]["managed_providers"][0]["model_capabilities"] == [declared]


def test_readding_removed_provider_clears_pending_restart(tmp_path, monkeypatch):
    client, _ = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}
    provider = {
        "name": "admin-a",
        "type": "openai",
        "api_key": "",
        "base_url": "",
        "default_model": None,
    }
    monkeypatch.setattr(
        "coworker.brain.factory.build_provider",
        lambda type_, api_key, **kwargs: SimpleNamespace(
            provider_name=kwargs.get("name") or type_
        ),
    )
    client.patch(
        "/api/admin/config",
        headers=headers,
        json={
            "changes": {"llm": {"managed_providers": [provider]}},
            "secrets": {"llm.managed_providers.0.api_key": "sk-a"},
        },
    )
    removed = client.patch(
        "/api/admin/config",
        headers=headers,
        json={"changes": {"llm": {"managed_providers": []}}},
    )

    restored = client.patch(
        "/api/admin/config",
        headers=headers,
        json={
            "changes": {"llm": {"managed_providers": [provider]}},
            "secrets": {"llm.managed_providers.0.api_key": "sk-a"},
        },
    )

    assert removed.json()["pending_restart"] is True
    assert restored.json()["pending_restart"] is False


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


def test_config_patch_marks_public_url_for_restart(tmp_path):
    client, _ = _client(tmp_path)

    response = client.patch(
        "/api/admin/config",
        headers={"Authorization": "Bearer secret"},
        json={"changes": {"api": {"public_url": "https://coworker.example.com"}}},
    )

    assert response.status_code == 200
    assert response.json()["requires_restart"] == ["api.public_url"]
    assert response.json()["pending_restart"] is True


def test_config_patch_persists_only_changed_fields(tmp_path):
    client, _ = _client(tmp_path)

    response = client.patch(
        "/api/admin/config",
        headers={"Authorization": "Bearer secret"},
        json={"changes": {"agent": {"idle_sleep_seconds": 12}}, "secrets": {}},
    )

    assert response.status_code == 200
    saved = json.loads((tmp_path / "admin_config.json").read_text(encoding="utf-8"))
    assert saved == {"agent": {"idle_sleep_seconds": 12}}


def test_config_patch_removes_historical_default_snapshot(tmp_path):
    path = tmp_path / "admin_config.json"
    path.write_text(
        json.dumps(
            {
                "agent": {
                    "passive_mode": False,
                    "bubble_handoff_transparency_participant_matches": [
                        "wecom:*",
                        "weixin:*",
                        "coworker-desktop:*:local:*",
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    client, _ = _client(tmp_path)

    response = client.patch(
        "/api/admin/config",
        headers={"Authorization": "Bearer secret"},
        json={"changes": {}, "secrets": {}},
    )

    assert response.status_code == 200
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_config_patch_removes_value_restored_to_inherited_config(tmp_path):
    path = tmp_path / "admin_config.json"
    path.write_text(
        json.dumps({"api": {"port": 8123}}),
        encoding="utf-8",
    )
    client, config = _client(tmp_path)
    config.api.port = 8123

    response = client.patch(
        "/api/admin/config",
        headers={"Authorization": "Bearer secret"},
        json={"changes": {"api": {"port": 8000}}, "secrets": {}},
    )

    assert response.status_code == 200
    assert response.json()["requires_restart"] == ["api.port"]
    assert response.json()["pending_restart"] is True
    assert json.loads(path.read_text(encoding="utf-8")) == {}
    desired = client.get(
        "/api/admin/config",
        headers={"Authorization": "Bearer secret"},
    ).json()
    assert desired["config"]["api"]["port"] == 8000


def test_config_patch_clears_pending_restart_after_reverting_to_running_value(tmp_path):
    client, _ = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}
    client.patch(
        "/api/admin/config",
        headers=headers,
        json={"changes": {"api": {"port": 8123}}},
    )

    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={"changes": {"api": {"port": 8000}}},
    )

    assert response.json()["requires_restart"] == []
    assert response.json()["pending_restart"] is False


def test_config_response_identifies_overridden_fields(tmp_path):
    path = tmp_path / "admin_config.json"
    path.write_text(
        json.dumps({"api": {"port": 8123}, "agent": {"passive_mode": True}}),
        encoding="utf-8",
    )
    client, _ = _client(tmp_path)

    response = client.get(
        "/api/admin/config",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.json()["overridden_fields"] == [
        "agent.passive_mode",
        "api.port",
    ]


def test_config_patch_explicitly_clears_one_override(tmp_path):
    path = tmp_path / "admin_config.json"
    client, config = _client(tmp_path)
    retained_host = "0.0.0.0" if config.api.host != "0.0.0.0" else "127.0.0.1"
    path.write_text(
        json.dumps({"api": {"host": retained_host, "port": 8123}}),
        encoding="utf-8",
    )
    config.api.port = 8123

    response = client.patch(
        "/api/admin/config",
        headers={"Authorization": "Bearer secret"},
        json={"clear_overrides": ["api.port"]},
    )

    assert response.status_code == 200
    assert json.loads(path.read_text(encoding="utf-8")) == {"api": {"host": retained_host}}


def test_config_patch_rejects_unknown_clear_override(tmp_path):
    client, _ = _client(tmp_path)

    response = client.patch(
        "/api/admin/config",
        headers={"Authorization": "Bearer secret"},
        json={"clear_overrides": ["api.missing"]},
    )

    assert response.status_code == 400


def test_config_patch_preserves_explicit_empty_list_override(tmp_path):
    client, _ = _client(tmp_path)

    response = client.patch(
        "/api/admin/config",
        headers={"Authorization": "Bearer secret"},
        json={
            "changes": {
                "agent": {
                    "bubble_handoff_transparency_participant_matches": [],
                }
            },
            "secrets": {},
        },
    )

    assert response.status_code == 200
    saved = json.loads((tmp_path / "admin_config.json").read_text(encoding="utf-8"))
    assert saved == {
        "agent": {"bubble_handoff_transparency_participant_matches": []}
    }


def test_wecom_config_hot_reconnects_and_preserves_secret(tmp_path):
    runner = SimpleNamespace(reconfigure=AsyncMock())
    modules = ChannelModuleRegistry()
    modules.register(
        WeComModule(
            channel=WeComChannel(runner),
            runtime=runner,
            settings=WeComSettings(runner),
        )
    )
    client, config = _client(
        tmp_path,
        wecom={"enabled": True, "bot_id": "old", "secret": "existing"},
        channel_modules=modules,
    )
    headers = {"Authorization": "Bearer secret"}

    body = client.get("/api/admin/config", headers=headers).json()
    assert "wecom" in body["hot_reloadable"]
    assert body["secret_status"]["wecom.secret"]["last4"] == "ting"

    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={
            "changes": {
                "wecom": {
                    "enabled": True,
                    "bot_id": "new",
                    "secret": "",
                    "ws_url": "wss://wecom.example/ws",
                }
            },
            "secrets": {},
        },
    )

    assert response.status_code == 200
    assert response.json()["applied_now"] == ["wecom"]
    assert response.json()["requires_restart"] == []
    assert response.json()["pending_restart"] is False
    assert config.wecom.bot_id == "new"
    assert config.wecom.secret == "existing"
    runner.reconfigure.assert_awaited_once()
    applied = runner.reconfigure.await_args.args[0]
    assert applied.ws_url == "wss://wecom.example/ws"
    assert applied.secret == "existing"


def test_mem0_llm_config_hot_applies_and_reconfigures(tmp_path):
    long_term = SimpleNamespace(reconfigure=AsyncMock())
    client, config = _client(tmp_path, long_term=long_term)
    headers = {"Authorization": "Bearer secret"}

    body = client.get("/api/admin/config", headers=headers).json()
    for path in (
        "memory.mem0_llm_provider",
        "memory.mem0_llm_model",
        "memory.mem0_llm_thinking",
    ):
        assert path in body["hot_reloadable"]

    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={
            "changes": {
                "memory": {
                    "mem0_llm_provider": "qwen",
                    "mem0_llm_model": "qwen3.6-flash",
                    "mem0_llm_thinking": True,
                }
            },
            "secrets": {},
        },
    )

    assert response.status_code == 200
    assert response.json()["requires_restart"] == []
    assert response.json()["pending_restart"] is False
    long_term.reconfigure.assert_awaited_once()
    applied = long_term.reconfigure.await_args.args[0]
    assert applied.provider == "qwen"
    assert applied.model == "qwen3.6-flash"
    assert applied.thinking is True
    assert config.memory.mem0_llm_model == "qwen3.6-flash"


def test_model_orchestration_reads_and_updates_mem0(tmp_path):
    long_term = SimpleNamespace(reconfigure=AsyncMock())
    client, config = _client(tmp_path, long_term=long_term)
    headers = {"Authorization": "Bearer secret"}

    body = client.get("/api/admin/model", headers=headers).json()
    assert body["mem0"] == {"provider": "", "model": "", "thinking": False}

    response = client.patch(
        "/api/admin/model",
        headers=headers,
        json={
            "summary": {"provider": "", "model": "", "thinking": False},
            "fallbacks": [],
            "vision": {"provider": "", "model": "", "thinking": True},
            "mem0": {"provider": "qwen", "model": "qwen3.6-flash", "thinking": True},
        },
    )

    assert response.status_code == 200
    assert response.json()["mem0"] == {
        "provider": "qwen",
        "model": "qwen3.6-flash",
        "thinking": True,
    }
    long_term.reconfigure.assert_awaited_once()
    applied = long_term.reconfigure.await_args.args[0]
    assert applied.provider == "qwen"
    assert applied.model == "qwen3.6-flash"
    assert applied.thinking is True
    assert config.memory.mem0_llm_provider == "qwen"


def test_channel_access_config_hot_applies_with_direct_channel_shape(tmp_path):
    client, config = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}
    access = ChannelAccessController(config.channel_access)

    body = client.get("/api/admin/config", headers=headers).json()
    assert body["config"]["channel_access"] == {}
    assert "channel_access" in body["hot_reloadable"]

    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={
            "changes": {
                "channel_access": {
                    "wecom": {
                        "inbound_allow": ["wecom:single:*"],
                        "inbound_deny": ["wecom:single:blocked"],
                        "outbound_allow": [],
                        "outbound_deny": ["wecom:group:test-*"],
                    }
                }
            },
            "secrets": {},
        },
    )

    assert response.status_code == 200
    assert response.json()["applied_now"] == ["channel_access"]
    assert response.json()["requires_restart"] == []
    rules = config.channel_access.root["wecom"]
    assert rules.inbound_allow == ["wecom:single:*"]
    assert rules.outbound_deny == ["wecom:group:test-*"]
    assert not access.allows("wecom", "outbound", "wecom:group:test-one")
    saved = json.loads((tmp_path / "admin_config.json").read_text(encoding="utf-8"))
    assert saved["channel_access"]["wecom"] == {
        "inbound_allow": ["wecom:single:*"],
        "inbound_deny": ["wecom:single:blocked"],
        "outbound_allow": [],
        "outbound_deny": ["wecom:group:test-*"],
    }

    cleared = client.patch(
        "/api/admin/config",
        headers=headers,
        json={"clear_overrides": ["channel_access.wecom"]},
    )

    assert cleared.status_code == 200
    assert cleared.json()["applied_now"] == ["channel_access"]
    assert config.channel_access.root == {}
    assert access.allows("wecom", "outbound", "wecom:group:test-one")
    assert json.loads(
        (tmp_path / "admin_config.json").read_text(encoding="utf-8")
    ) == {}


def test_channel_traffic_is_authenticated_and_filterable(tmp_path):
    client, config = _client(tmp_path)
    path = Path(config.agent.logs_dir) / "channel_traffic.jsonl"
    traffic = ChannelTrafficStore(path)
    traffic.record(
        direction="inbound",
        channel="wecom",
        participant_id="wecom:single:blocked",
        status="denied",
        source="wecom",
        reason="policy",
    )
    traffic.record(
        direction="outbound",
        channel="desktop",
        participant_id="coworker-desktop:desk:local:one",
        status="sent",
        source="agent",
    )

    assert client.get("/api/admin/channel-traffic").status_code == 401
    response = client.get(
        "/api/admin/channel-traffic?direction=inbound&status=denied&channel=wecom",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert response.json()["entries"] == [
        {
            "ts": response.json()["entries"][0]["ts"],
            "direction": "inbound",
            "channel": "wecom",
            "participant_id": "wecom:single:blocked",
            "status": "denied",
            "source": "wecom",
            "reason": "policy",
        }
    ]
    assert "message" not in response.text

    invalid = client.get(
        "/api/admin/channel-traffic?status=unknown",
        headers={"Authorization": "Bearer secret"},
    )
    assert invalid.status_code == 422


class _ChannelManagement:
    async def snapshot(self) -> dict[str, object]:
        return {"connections": [{"bot_instance_id": "bot-1"}]}

    async def execute(
        self,
        command: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        return {"command": command, "payload": payload}


class _ChannelSettings:
    config_key = "weixin"

    def __init__(self) -> None:
        self.applied: list[object] = []

    async def apply(self, config: object) -> None:
        self.applied.append(config)


def _channel_modules(
    *,
    management: object | None = None,
    settings: object | None = None,
) -> ChannelModuleRegistry:
    modules = ChannelModuleRegistry()
    modules.register(
        SimpleNamespace(
            name="weixin",
            channel=SimpleNamespace(name="weixin"),
            management=management,
            settings=settings,
        )
    )
    return modules


def test_channel_management_api_routes_without_channel_specific_admin_code(tmp_path):
    modules = _channel_modules(management=_ChannelManagement())
    client, _ = _client(tmp_path, channel_modules=modules)
    headers = {"Authorization": "Bearer secret"}

    snapshot = client.get(
        "/api/admin/channels/weixin/management",
        headers=headers,
    )
    command = client.post(
        "/api/admin/channels/weixin/management/remove_connection",
        headers=headers,
        json={"bot_instance_id": "bot-1", "confirm": True},
    )

    assert snapshot.json()["connections"][0]["bot_instance_id"] == "bot-1"
    assert command.json()["command"] == "remove_connection"
    assert command.json()["payload"]["confirm"] is True


def test_registered_channel_settings_are_hot_applied_generically(tmp_path):
    settings = _ChannelSettings()
    modules = _channel_modules(settings=settings)
    client, config = _client(
        tmp_path,
        channel_modules=modules,
        weixin={"enabled": False},
    )
    headers = {"Authorization": "Bearer secret"}

    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={"changes": {"weixin": {"enabled": True}}, "secrets": {}},
    )

    assert response.json()["applied_now"] == ["weixin"]
    assert config.weixin.enabled is True
    assert settings.applied[0].enabled is True


def test_runtime_language_round_trip_requires_restart(tmp_path):
    client, config = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}

    body = client.get("/api/admin/config", headers=headers).json()
    assert body["config"]["i18n"] == {"locale": "zh-CN"}
    assert "i18n.locale" not in body["hot_reloadable"]

    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={"changes": {"i18n": {"locale": "en-US"}}, "secrets": {}},
    )
    assert response.status_code == 200
    assert response.json()["applied_now"] == []
    assert response.json()["requires_restart"] == ["i18n.locale"]
    assert config.i18n.locale.value == "zh-CN"

    saved = json.loads((tmp_path / "admin_config.json").read_text(encoding="utf-8"))
    assert saved["i18n"]["locale"] == "en-US"
    desired = client.get("/api/admin/config", headers=headers).json()
    assert desired["config"]["i18n"]["locale"] == "en"


def test_admin_overlay_has_higher_priority_than_base_config(tmp_path):
    path = tmp_path / "admin_config.json"
    path.write_text(json.dumps({"agent": {"idle_sleep_seconds": 7}}), encoding="utf-8")
    config = Config.model_validate(
        {
            "admin": {"config_file": str(path)},
            "agent": {"idle_sleep_seconds": 30},
        }
    )
    loaded = apply_admin_config_file(config)
    assert loaded.agent.idle_sleep_seconds == 7
    assert loaded.admin.config_file == str(path)


def test_admin_overlay_evolves_historical_handoff_defaults(tmp_path):
    path = tmp_path / "admin_config.json"
    path.write_text(
        json.dumps(
            {
                "agent": {
                    "bubble_handoff_transparency_participant_matches": [
                        "wecom:*",
                        "coworker-desktop:*:local:*",
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    config = Config.model_validate({"admin": {"config_file": str(path)}})

    loaded = apply_admin_config_file(config)

    assert loaded.agent.bubble_handoff_transparency_participant_matches == [
        "wecom:*",
        "weixin:*",
        "coworker-desktop:*:local:*",
    ]


def test_admin_overlay_preserves_custom_handoff_matches(tmp_path):
    path = tmp_path / "admin_config.json"
    custom_matches = ["wecom:alice", "coworker-desktop:*:local:*"]
    path.write_text(
        json.dumps(
            {
                "agent": {
                    "bubble_handoff_transparency_participant_matches": custom_matches
                }
            }
        ),
        encoding="utf-8",
    )
    config = Config.model_validate({"admin": {"config_file": str(path)}})

    loaded = apply_admin_config_file(config)

    assert loaded.agent.bubble_handoff_transparency_participant_matches == custom_matches


def test_admin_overlay_preserves_disabled_handoff_matches(tmp_path):
    path = tmp_path / "admin_config.json"
    path.write_text(
        json.dumps(
            {"agent": {"bubble_handoff_transparency_participant_matches": []}}
        ),
        encoding="utf-8",
    )
    config = Config.model_validate({"admin": {"config_file": str(path)}})

    loaded = apply_admin_config_file(config)

    assert loaded.agent.bubble_handoff_transparency_participant_matches == []


def test_startup_normalization_migrates_old_default_snapshot(tmp_path):
    path = tmp_path / "admin_config.json"
    path.write_text(
        json.dumps(
            {
                "admin": {"token": "stored-secret"},
                "agent": {
                    "idle_sleep_seconds": 12,
                    "passive_mode": False,
                    "bubble_handoff_transparency_participant_matches": [
                        "wecom:*",
                        "coworker-desktop:*:local:*",
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    inherited = Config.model_validate({"admin": {"config_file": str(path)}})

    migrated = normalize_admin_overrides_file(inherited)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert (migrated, saved) == (
        True,
        {
            "admin": {"token": "stored-secret"},
            "agent": {"idle_sleep_seconds": 12},
        },
    )


def test_startup_normalization_preserves_explicit_empty_list(tmp_path):
    path = tmp_path / "admin_config.json"
    overrides = {
        "agent": {"bubble_handoff_transparency_participant_matches": []}
    }
    path.write_text(json.dumps(overrides), encoding="utf-8")
    inherited = Config.model_validate({"admin": {"config_file": str(path)}})

    migrated = normalize_admin_overrides_file(inherited)

    assert (migrated, json.loads(path.read_text(encoding="utf-8"))) == (
        False,
        overrides,
    )


def test_first_run_admin_token_is_generated_and_preserves_overrides(tmp_path):
    path = tmp_path / "admin_config.json"
    path.write_text(json.dumps({"agent": {"tick": False}}), encoding="utf-8")
    config = Config.model_validate(
        {
            "admin": {"token": "", "config_file": str(path)},
            "desktop_updates": {"admin_token": ""},
        }
    )

    token = ensure_admin_token(config)

    assert token
    assert config.admin.token == token
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["admin"]["token"] == token
    assert saved["agent"]["tick"] is False
    assert ensure_admin_token(config) is None


def test_effective_admin_token_prefers_admin_and_falls_back_to_desktop(tmp_path):
    config = Config.model_validate(
        {
            "admin": {"token": "admin-token", "config_file": str(tmp_path / "admin.json")},
            "desktop_updates": {"admin_token": "legacy-token"},
        }
    )
    assert effective_admin_token(config) == "admin-token"
    assert ensure_admin_token(config) is None

    config.admin.token = ""
    assert effective_admin_token(config) == "legacy-token"
    assert ensure_admin_token(config) is None

    config.desktop_updates.admin_token = ""
    assert effective_admin_token(config) == ""


def test_effective_communication_token_prefers_dedicated_token_and_falls_back_to_admin(
    tmp_path,
):
    config = Config.model_validate(
        {
            "api": {"communication_token": "desktop-token"},
            "admin": {
                "token": "admin-token",
                "config_file": str(tmp_path / "admin.json"),
            },
        }
    )

    assert effective_communication_token(config) == "desktop-token"

    config.api.communication_token = ""

    assert effective_communication_token(config) == "admin-token"


def test_setup_admin_token_banner_shows_existing_effective_token(tmp_path, capsys):
    config = Config.model_validate(
        {
            "admin": {"token": "saved-token", "config_file": str(tmp_path / "admin.json")},
            "api": {"port": 8123},
        }
    )

    with locale_context("en"):
        _print_setup_admin_token(config)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "saved-token" in captured.err
    assert "http://127.0.0.1:8123/admin" in captured.err

    config.api.public_url = "https://coworker.example.com"
    _print_setup_admin_token(config)
    captured = capsys.readouterr()
    assert "https://coworker.example.com/admin" in captured.err
    assert "http://127.0.0.1:8123/admin" not in captured.err

    config.admin.token = ""
    config.desktop_updates.admin_token = "legacy-token"
    _print_setup_admin_token(config)
    assert "legacy-token" in capsys.readouterr().err

    config.desktop_updates.admin_token = ""
    _print_setup_admin_token(config)
    assert capsys.readouterr().err == ""


def test_bootstrap_persists_first_provider_and_runtime_defaults(tmp_path, monkeypatch):
    client, config = _client(tmp_path)
    admin._brain.active_provider = None
    admin._agent._identity._dir = tmp_path / "identity"
    admin._agent._identity.load = lambda: None
    monkeypatch.setattr(
        admin,
        "_server_timezone_description",
        lambda: "Asia/Shanghai (UTC+8)",
    )
    headers = {"Authorization": "Bearer secret"}

    status = client.get("/api/admin/bootstrap", headers=headers)
    assert status.status_code == 200
    assert status.json()["required"] is True
    assert status.json()["server_timezone"] == "Asia/Shanghai (UTC+8)"
    defaults = status.json()["defaults"]
    assert defaults["configuration"]["llm"]["max_tokens"] == 8192
    assert defaults["configuration"]["memory"]["short_term_max_tokens"] == 120_000
    assert defaults["configuration"]["agent"]["passive_mode"] is False
    assert defaults["configuration"]["i18n"]["locale"] == "zh-CN"
    assert "timezone" not in defaults["configuration"]["i18n"]
    assert defaults["configuration"]["admin"]["token"] == ""
    assert defaults["secret_status"]["admin.token"] == {
        "configured": True,
        "last4": "cret",
    }
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
            "reconnect_proof": "ab" * 32,
            "configuration": {
                "llm": {
                    "max_tokens": 4096,
                    "summary_provider": "openai",
                    "summary_model": "gpt-5.2",
                },
                "memory": {
                    "short_term_max_tokens": 48_000,
                    "compress_ratio": 0.4,
                    "tree_backfill_max_leaves": 32,
                    "auto_recall_enabled": False,
                    "auto_recall_relevance_threshold": 0.72,
                    "auto_recall_limit": 3,
                    "persona_enabled": False,
                },
                "i18n": {"locale": "en"},
                "agent": {
                    "passive_mode": True,
                    "idle_sleep_seconds": 90,
                    "bubble_max_concurrent": 2,
                    "inbox_batch_max": 4,
                },
                "api": {
                    "port": 8124,
                    "public_url": "https://coworker.example.com",
                    "development_mode": True,
                    "cors_origins": ["https://desktop.example"],
                },
                "relay": {
                    "enabled": False,
                    "url": "https://relay.example.test",
                    "instance_id": "cw_abcdefgh",
                    "auth_epoch": 2,
                },
                "channel_access": {
                    "wecom": {"inbound_allow": ["wecom:single:*"]}
                },
                "wecom": {
                    "enabled": True,
                    "bot_id": "bot-first-run",
                    "ws_url": "wss://wecom.example.test/ws",
                },
                "weixin": {"enabled": False},
                "desktop_updates": {
                    "sync_interval_seconds": 600,
                    "sync_on_start": False,
                },
            },
            "secrets": {
                "api.communication_token": "desktop-first-run",
                "relay.instance_private_key": "relay-private",
                "wecom.secret": "wecom-first-run",
            },
        },
    )

    assert response.status_code == 202
    saved = json.loads((tmp_path / "admin_config.json").read_text(encoding="utf-8"))
    assert saved["llm"]["default_provider"] == "openai"
    assert saved["llm"]["default_model"] == "gpt-5.2"
    assert saved["llm"]["max_tokens"] == 4096
    assert saved["llm"]["summary_provider"] == "openai"
    assert saved["llm"]["managed_providers"][0]["api_key"] == "sk-first-run"
    assert saved["memory"]["short_term_max_tokens"] == 48_000
    assert saved["memory"]["compress_ratio"] == 0.4
    assert saved["memory"]["tree_backfill_max_leaves"] == 32
    assert saved["memory"]["auto_recall_enabled"] is False
    assert saved["memory"]["auto_recall_relevance_threshold"] == 0.72
    assert saved["memory"]["auto_recall_limit"] == 3
    assert saved["memory"]["persona_enabled"] is False
    assert saved["i18n"]["locale"] == "en"
    assert saved["agent"]["passive_mode"] is True
    assert saved["agent"]["idle_sleep_seconds"] == 90
    assert saved["agent"]["bubble_max_concurrent"] == 2
    assert saved["agent"]["inbox_batch_max"] == 4
    assert saved["api"]["port"] == 8124
    assert saved["api"]["public_url"] == "https://coworker.example.com"
    assert saved["api"]["communication_token"] == "desktop-first-run"
    assert saved["relay"]["instance_id"] == "cw_abcdefgh"
    assert saved["relay"]["instance_private_key"] == "relay-private"
    assert saved["channel_access"]["wecom"]["inbound_allow"] == ["wecom:single:*"]
    assert saved["wecom"]["secret"] == "wecom-first-run"
    assert saved["weixin"]["enabled"] is False
    assert saved["desktop_updates"]["sync_interval_seconds"] == 600
    assert (tmp_path / "identity" / "name.txt").read_text(encoding="utf-8") == "Nova"
    intent = json.loads(
        (tmp_path / "memory" / "startup_intent.json").read_text(encoding="utf-8")
    )
    assert intent == {
        "version": 1,
        "reason": "bootstrap",
        "provider": "openai",
        "model": "gpt-5.2",
        "reconnect_proof": "ab" * 32,
    }
    assert "sk-first-run" not in json.dumps(intent)
    assert config.admin.token == "secret"


def test_bootstrap_requires_confirmation_for_custom_model(tmp_path):
    client, _ = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}
    payload = {
        "provider_type": "openai",
        "model": "custom-tool-model",
        "api_key": "sk-test",
    }
    assert client.post("/api/admin/bootstrap", headers=headers, json=payload).status_code == 409

    admin._brain.active_provider = None
    rejected = client.post("/api/admin/bootstrap", headers=headers, json=payload)
    assert rejected.status_code == 422
    assert not (tmp_path / "admin_config.json").exists()

    accepted = client.post(
        "/api/admin/bootstrap",
        headers=headers,
        json={
            **payload,
            "model_capabilities": {"tools": True, "vision": True, "video": False},
        },
    )
    assert accepted.status_code == 202
    saved = json.loads((tmp_path / "admin_config.json").read_text(encoding="utf-8"))
    assert saved["llm"]["default_model"] == "custom-tool-model"
    assert saved["llm"]["managed_providers"][0]["model_capabilities"] == [
        {
            "model": "custom-tool-model",
            "tools": True,
            "vision": True,
            "video": False,
        }
    ]
    assert "max_tokens" not in saved["llm"]
    assert "i18n" not in saved
    blocked_patch = client.patch(
        "/api/admin/config",
        headers=headers,
        json={"changes": {"llm": {"default_model": "gpt-5.2"}}},
    )
    assert blocked_patch.status_code == 409


def test_bootstrap_failure_before_commit_does_not_leave_startup_intent(tmp_path):
    client, _ = _client(tmp_path)
    admin._brain.active_provider = None
    admin._agent._identity._dir = tmp_path / "identity"

    def fail_identity_load():
        raise RuntimeError("identity load failed")

    admin._agent._identity.load = fail_identity_load

    try:
        client.post(
            "/api/admin/bootstrap",
            headers={"Authorization": "Bearer secret"},
            json={
                "provider_type": "openai",
                "model": "gpt-5.2",
                "api_key": "sk-test",
                "coworker_name": "Nova",
            },
        )
    except RuntimeError as error:
        assert str(error) == "identity load failed"
    else:
        raise AssertionError("bootstrap should propagate the identity failure")

    assert not (tmp_path / "admin_config.json").exists()
    assert not (tmp_path / "memory" / "startup_intent.json").exists()
    assert not (tmp_path / "identity" / "name.txt").exists()


def test_bootstrap_config_write_failure_clears_startup_intent(tmp_path, monkeypatch):
    client, _ = _client(tmp_path)
    admin._brain.active_provider = None
    admin._agent._identity._dir = tmp_path / "identity"
    admin._agent._identity.load = lambda: None
    (tmp_path / "identity").mkdir()
    (tmp_path / "identity" / "name.txt").write_text("Luna", encoding="utf-8")

    def fail_config_write(path, payload):
        raise OSError("config write failed")

    monkeypatch.setattr(
        admin._require_admin_config_service(),
        "write_sparse_overrides",
        fail_config_write,
    )
    try:
        client.post(
            "/api/admin/bootstrap",
            headers={"Authorization": "Bearer secret"},
            json={
                "provider_type": "openai",
                "model": "gpt-5.2",
                "api_key": "sk-test",
                "coworker_name": "Nova",
            },
        )
    except OSError as error:
        assert str(error) == "config write failed"
    else:
        raise AssertionError("bootstrap should propagate the config write failure")

    assert not (tmp_path / "memory" / "startup_intent.json").exists()
    assert (tmp_path / "identity" / "name.txt").read_text(encoding="utf-8") == "Luna"


def test_bootstrap_custom_model_confirmation_does_not_trust_provider_capability(tmp_path):
    client, _ = _client(tmp_path)
    admin._brain.active_provider = None
    response = client.post(
        "/api/admin/bootstrap",
        headers={"Authorization": "Bearer secret"},
        json={
            "provider_type": "anthropic",
            "model": "future-claude-model",
            "api_key": "sk-test",
        },
    )
    assert response.status_code == 422


def test_bootstrap_custom_primary_model_requires_declared_tool_support(tmp_path):
    client, _ = _client(tmp_path)
    admin._brain.active_provider = None

    response = client.post(
        "/api/admin/bootstrap",
        headers={"Authorization": "Bearer secret"},
        json={
            "provider_type": "openai",
            "model": "custom-vision-model",
            "api_key": "sk-test",
            "model_capabilities": {"tools": False, "vision": True, "video": False},
        },
    )

    assert response.status_code == 422
    assert "必须支持工具调用" in response.json()["detail"]
    assert not (tmp_path / "admin_config.json").exists()


def test_bootstrap_rejects_invalid_runtime_options_and_blank_credentials(tmp_path):
    client, _ = _client(tmp_path)
    admin._brain.active_provider = None
    headers = {"Authorization": "Bearer secret"}
    base = {"provider_type": "openai", "model": "gpt-5.2", "api_key": "sk-test"}

    assert client.post(
        "/api/admin/bootstrap",
        headers=headers,
        json={**base, "max_tokens": 2048},
    ).status_code == 422
    assert client.post(
        "/api/admin/bootstrap",
        headers=headers,
        json={**base, "configuration": {"llm": {"max_tokens": 0}}},
    ).status_code == 422
    assert client.post(
        "/api/admin/bootstrap",
        headers=headers,
        json={**base, "configuration": {"api": {"port": 65_536}}},
    ).status_code == 422
    assert client.post(
        "/api/admin/bootstrap",
        headers=headers,
        json={**base, "configuration": {"api": {"public_url": "https://example.com/path"}}},
    ).status_code == 422
    assert client.post(
        "/api/admin/bootstrap",
        headers=headers,
        json={**base, "configuration": {"i18n": {"locale": "fr"}}},
    ).status_code == 422
    assert client.post(
        "/api/admin/bootstrap", headers=headers, json={**base, "model": "   "}
    ).status_code == 422
    assert client.post(
        "/api/admin/bootstrap", headers=headers, json={**base, "api_key": "   "}
    ).status_code == 422
    assert client.post(
        "/api/admin/bootstrap",
        headers=headers,
        json={**base, "configuration": {"memory": {"compress_ratio": 1}}},
    ).status_code == 422
    assert client.post(
        "/api/admin/bootstrap",
        headers=headers,
        json={
            **base,
            "configuration": {
                "memory": {"auto_recall_relevance_threshold": 1.1}
            },
        },
    ).status_code == 422
    assert client.post(
        "/api/admin/bootstrap",
        headers=headers,
        json={**base, "configuration": {"llm": {"default_model": "managed"}}},
    ).status_code == 422
    assert client.post(
        "/api/admin/bootstrap",
        headers=headers,
        json={**base, "secrets": {"admin.token": "replacement"}},
    ).status_code == 422
    assert not (tmp_path / "admin_config.json").exists()
    assert not (tmp_path / "memory" / "startup_intent.json").exists()


def test_overview_uses_short_term_configured_token_capacity(tmp_path):
    client, config = _client(tmp_path)
    config.agent.passive_mode = True
    config.agent.idle_sleep_seconds = 0
    status_path = Path(config.memory.db_path) / "instance_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps({"startup_reason": "bootstrap"}), encoding="utf-8"
    )
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

    response = client.get("/api/admin/overview", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert response.json()["memory"]["max_tokens"] == 12_345
    assert response.json()["status"]["passive_mode"] is True
    assert response.json()["status"]["idle_sleep_seconds"] == 0
    assert response.json()["status"]["startup_reason"] == "bootstrap"


def test_bubble_history_survives_restart_and_preserves_raw_values(tmp_path):
    client, config = _client(tmp_path)
    bubble_dir = Path(config.agent.logs_dir) / "bubbles"
    bubble_dir.mkdir(parents=True)
    path = bubble_dir / "bbl_260716120000.jsonl"
    entries = [
        {"type": "message_in", "content": "原始用户消息", "ts": "2026-07-16T12:00:00"},
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
            "max_cycles": 4,
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
    subconscious_path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries),
        encoding="utf-8",
    )
    response = client.get("/api/admin/subconscious", headers=headers)
    assert response.status_code == 200
    assert response.json()["bubbles"][0]["mode"] == "audit"
    response = client.get("/api/admin/subconscious/bbl_260716120000_audit/history", headers=headers)
    assert response.status_code == 200
    assert len(response.json()["events"]) == 4

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


def test_only_terminal_bubble_logs_are_cached(tmp_path):
    from coworker.api.admin import router_module as admin

    active = tmp_path / "active.jsonl"
    active.write_text(
        '{"__meta__":true,"id":"active","status":"timeout"}\n'
        '{"type":"thinking_start"}\n',
        encoding="utf-8",
    )
    completed = tmp_path / "completed.jsonl"
    completed.write_text(
        '{"type":"thinking_start"}\n'
        '{"__meta__":true,"id":"completed","status":"done"}\n',
        encoding="utf-8",
    )

    admin._bubble_log_summary_cached.cache_clear()
    admin._read_completed_bubble_log_cached.cache_clear()
    assert admin._read_bubble_log_summary(active) is not None
    assert admin._bubble_log_summary_cached.cache_info().currsize == 0
    assert admin._read_bubble_log(active)
    assert admin._read_completed_bubble_log_cached.cache_info().currsize == 0

    assert admin._read_bubble_log_summary(completed) is not None
    assert admin._bubble_log_summary_cached.cache_info().currsize == 1
    assert admin._read_bubble_log(completed)
    assert admin._read_completed_bubble_log_cached.cache_info().currsize == 1


def test_completed_bubble_index_avoids_rescanning_legacy_logs(tmp_path, monkeypatch):
    from coworker.agent.bubble_log_index import (
        load_completed_bubble_index,
        upsert_completed_bubble_index,
    )
    from coworker.api.admin import router_module as admin

    log_dir = tmp_path / "bubbles"
    log_dir.mkdir()
    completed_log = log_dir / "bbl_complete.jsonl"
    completed_log.write_text(
        '{"type":"thinking_start","ts":"2026-07-16T12:00:00"}\n'
        '{"__meta__":true,"id":"bbl_complete","status":"done"}\n',
        encoding="utf-8",
    )
    (log_dir / "bbl_active.jsonl").write_text(
        '{"type":"thinking_start","ts":"2026-07-16T12:00:00"}\n',
        encoding="utf-8",
    )
    (log_dir / "already_indexed.jsonl").write_text(
        '{"__meta__":true,"id":"already_indexed","status":"done"}\n',
        encoding="utf-8",
    )
    upsert_completed_bubble_index(tmp_path, {"log_id": "already_indexed", "id": "already_indexed"})
    upsert_completed_bubble_index(tmp_path, {"log_id": "missing", "id": "missing"})
    original_summary = admin._bubble_log_summary
    scanned: list[Path] = []

    def record_summary_scan(path):
        scanned.append(path)
        return original_summary(path)

    monkeypatch.setattr(admin, "_bubble_log_summary", record_summary_scan)
    rebuilt = admin._completed_bubble_summaries(log_dir)
    assert {item["id"] for item in rebuilt} == {"already_indexed", "bbl_complete"}
    assert scanned == [completed_log]
    assert (tmp_path / "bubble_index.json").is_file()
    persisted = load_completed_bubble_index(tmp_path)
    assert persisted is not None
    assert {item["id"] for item in persisted} == {"already_indexed", "bbl_complete"}

    monkeypatch.setattr(admin, "_bubble_log_summary", lambda _path: pytest.fail("unexpected rescan"))
    indexed = admin._completed_bubble_summaries(log_dir)
    assert {item["id"] for item in indexed} == {"already_indexed", "bbl_complete"}

    for path in log_dir.iterdir():
        path.unlink()
    log_dir.rmdir()
    assert admin._completed_bubble_summaries(log_dir) == []
    assert load_completed_bubble_index(tmp_path) == []


def test_v1_bubble_index_rebuilds_model_from_legacy_log(tmp_path):
    from coworker.agent.bubble_log_index import load_completed_bubble_index
    from coworker.api.admin import router_module as admin

    log_dir = tmp_path / "bubbles"
    log_dir.mkdir()
    (log_dir / "legacy.jsonl").write_text(
        '{"type":"llm_response","provider":"openai","model":"gpt-5.2"}\n'
        '{"__meta__":true,"id":"legacy","status":"done","provider":"","model":""}\n',
        encoding="utf-8",
    )
    (tmp_path / "bubble_index.json").write_text(
        '{"version":1,"records":{"legacy":{"log_id":"legacy","model":""}}}',
        encoding="utf-8",
    )

    rebuilt = admin._completed_bubble_summaries(log_dir)

    assert rebuilt[0]["provider"] == "openai"
    assert rebuilt[0]["model"] == "gpt-5.2"
    assert load_completed_bubble_index(tmp_path) == rebuilt
    assert json.loads((tmp_path / "bubble_index.json").read_text(encoding="utf-8"))[
        "version"
    ] == 2


def test_legacy_index_rebuild_preserves_a_concurrent_completion(tmp_path):
    from coworker.agent.bubble_log_index import (
        load_completed_bubble_index,
        synchronize_completed_bubble_index,
        upsert_completed_bubble_index,
    )

    log_dir = tmp_path / "bubbles"
    log_dir.mkdir()
    (log_dir / "new.jsonl").touch()
    (log_dir / "legacy.jsonl").touch()
    upsert_completed_bubble_index(tmp_path, {"log_id": "new", "id": "new"})
    synchronize_completed_bubble_index(
        tmp_path,
        log_dir,
        [{"log_id": "legacy", "id": "legacy"}],
    )
    records = load_completed_bubble_index(tmp_path)
    assert records is not None
    assert {record["id"] for record in records} == {"legacy", "new"}


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
        agent=agent,
        brain=brain,
        config=config,
        alarm_manager=None,
        skill_loader=None,
        palace_loader=None,
        mode_loader=None,
    )

    body = client.get(
        "/api/admin/memory/short-term",
        headers={"Authorization": "Bearer secret"},
    ).json()

    assert body["token_watermark"]["source"] == "estimated"
    assert body["token_watermark"]["tokens"] > 0
    assert (
        body["token_watermark"]["tokens"] == body["token_watermark"]["estimated_short_term_tokens"]
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


def test_short_term_messages_tail_is_lightweight_and_matches_full_snapshot(tmp_path):
    client, config = _client(tmp_path)
    short_term = ShortTermMemory(max_tokens=1_000)
    short_term.primary.append(Message(role="user", content="first"))
    short_term.primary.append(Message(role="assistant", content="second"))
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

    headers = {"Authorization": "Bearer secret"}
    full = client.get("/api/admin/memory/short-term", headers=headers).json()
    tail = client.get("/api/admin/memory/short-term/messages", headers=headers)

    assert tail.status_code == 200
    body = tail.json()
    assert set(body) == {"messages"}
    assert body["messages"] == full["messages"]


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
        agent=SimpleNamespace(_identity=_Identity()),
        brain=SimpleNamespace(),
        config=config,
        alarm_manager=None,
        skill_loader=SkillLoader(str(skills_dir)),
        palace_loader=None,
        mode_loader=None,
    )
    headers = {"Authorization": "Bearer secret"}

    response = client.get("/api/admin/content/skills/browser/files", headers=headers)
    assert [item["path"] for item in response.json()["files"]] == [
        "SKILL.md",
        "scripts/check.py",
    ]
    response = client.get(
        "/api/admin/content/skills/browser/files/scripts/check.py",
        headers=headers,
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
        "/api/admin/content/skills/browser/files/../outside.py",
        headers=headers,
    ).status_code in (400, 404)
    assert (
        client.delete(
            "/api/admin/content/skills/browser/files/SKILL.md",
            headers=headers,
        ).status_code
        == 409
    )
    assert (
        client.delete(
            "/api/admin/content/skills/browser",
            headers=headers,
        ).status_code
        == 200
    )
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
        {
            "type": "tool_result",
            "seq": 3,
            "ts": "2026-07-01T09:03:00",
            "name": "read_file",
            "content": "ok",
        },
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


def test_admin_interaction_history_filters_and_previews_memory_compressions(tmp_path):
    client, config = _client(tmp_path)
    logs_dir = Path(config.agent.logs_dir)
    logs_dir.mkdir(parents=True)
    entries = [
        {"type": "message_in", "seq": 0, "ts": "2026-07-01T09:00:00"},
        {
            "type": "memory_compression",
            "seq": 1,
            "ts": "2026-07-01T09:01:00",
            "trigger": "automatic",
            "mode": "incremental",
            "storage": "tree",
            "messages_compressed": 6,
            "duration_ms": 120,
            "summary_calls": 1,
            "summary_total_tokens": 80,
        },
    ]
    (logs_dir / "interactions.jsonl").write_text(
        "\n".join(json.dumps(item) for item in entries) + "\n",
        encoding="utf-8",
    )

    response = client.get(
        "/api/admin/interactions?event_type=memory_compression",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    events = response.json()["events"]
    assert [event["seq"] for event in events] == [1]
    assert events[0]["meta"] == {
        "mode": "incremental",
        "trigger": "automatic",
        "storage": "tree",
        "messages_compressed": "6",
        "duration_ms": "120",
        "summary_calls": "1",
        "summary_total_tokens": "80",
    }
    assert "messages_compressed" in events[0]["preview"]


def test_admin_interaction_history_can_jump_to_a_sequence_range(tmp_path):
    client, config = _client(tmp_path)
    logs_dir = Path(config.agent.logs_dir)
    logs_dir.mkdir(parents=True)
    (logs_dir / "interactions-000001.jsonl").write_text(
        "\n".join(
            json.dumps({"seq": seq, "ts": f"2026-07-01T09:0{seq}:00", "type": "message_in"})
            for seq in range(3)
        )
        + "\n",
        encoding="utf-8",
    )
    (logs_dir / "interactions.jsonl").write_text(
        "\n".join(
            json.dumps({"seq": seq, "ts": f"2026-07-01T09:0{seq}:00", "type": "tool_result"})
            for seq in range(3, 5)
        )
        + "\n",
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
    assert (
        client.get(
            "/api/admin/interactions?seq_start=4&seq_end=3",
            headers=headers,
        ).status_code
        == 400
    )


def test_admin_interaction_history_can_page_within_a_time_range(tmp_path):
    client, config = _client(tmp_path)
    logs_dir = Path(config.agent.logs_dir)
    logs_dir.mkdir(parents=True)
    entries = [
        {"seq": 0, "ts": "2026-07-01T08:59:59", "type": "message_in"},
        {"seq": 1, "ts": "2026-07-01T09:05:00", "type": "thinking_start"},
        {"seq": 2, "ts": "2026-07-01T09:30:00", "type": "llm_response"},
        {"seq": 3, "ts": "2026-07-01T10:00:00", "type": "tool_call"},
    ]
    (logs_dir / "interactions.jsonl").write_text(
        "\n".join(json.dumps(item) for item in entries) + "\n",
        encoding="utf-8",
    )
    headers = {"Authorization": "Bearer secret"}
    params = {
        "limit": "1",
        "start_time": "2026-07-01T09:00:00",
        "end_time": "2026-07-01T09:59:59.999999",
    }

    first = client.get("/api/admin/interactions", params=params, headers=headers)

    assert first.status_code == 200
    assert [item["seq"] for item in first.json()["events"]] == [2]
    time_range = first.json()["time_range"]
    assert time_range["start_time"].startswith("2026-07-01T09:00:00")
    assert time_range["end_time"].startswith("2026-07-01T09:59:59.999999")
    assert datetime.fromisoformat(time_range["start_time"]).utcoffset() is not None
    assert datetime.fromisoformat(time_range["end_time"]).utcoffset() is not None
    assert first.json()["next_cursor"]

    second = client.get(
        "/api/admin/interactions",
        params={**params, "cursor": first.json()["next_cursor"]},
        headers=headers,
    )

    assert second.status_code == 200
    assert [item["seq"] for item in second.json()["events"]] == [1]
    assert second.json()["has_more"] is False

    local_start = datetime(2026, 7, 1, 9, 0).astimezone()
    local_end = datetime(2026, 7, 1, 9, 59, 59, 999999).astimezone()
    absolute = client.get(
        "/api/admin/interactions",
        params={
            "start_time": local_start.astimezone(UTC).isoformat(),
            "end_time": local_end.astimezone(UTC).isoformat(),
        },
        headers=headers,
    )
    assert absolute.status_code == 200
    assert [item["seq"] for item in absolute.json()["events"]] == [2, 1]


def test_admin_interaction_history_validates_time_ranges(tmp_path):
    client, _ = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}

    with locale_context("zh-CN"):
        incomplete = client.get(
            "/api/admin/interactions?start_time=2026-07-01T09:00:00",
            headers=headers,
        )
        reversed_range = client.get(
            "/api/admin/interactions?start_time=2026-07-01T10:00:00"
            "&end_time=2026-07-01T09:00:00",
            headers=headers,
        )
        too_large = client.get(
            "/api/admin/interactions?start_time=2026-07-01T09:00:00"
            "&end_time=2026-07-02T09:00:00.000001",
            headers=headers,
        )

    assert incomplete.status_code == 422
    assert incomplete.json()["detail"] == "日志起止时间必须同时提供"
    assert reversed_range.status_code == 422
    assert reversed_range.json()["detail"] == "日志起始时间不能晚于结束时间"
    assert too_large.status_code == 422
    assert too_large.json()["detail"] == "日志时间范围不能超过 24 小时"


def test_legacy_admin_logs_endpoint_is_not_available(tmp_path):
    client, _ = _client(tmp_path)
    response = client.get("/api/admin/logs", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 404


def test_persona_disabled_returns_503(tmp_path):
    client, _ = _client(tmp_path, persona=False)
    response = client.get("/api/admin/persons", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 503
    assert client.get("/api/admin/persons").status_code == 401


def test_persons_crud_merge_and_card(tmp_path):
    client, _ = _client(tmp_path, persona=True)
    headers = {"Authorization": "Bearer secret"}

    created = client.post(
        "/api/admin/persons",
        json={},
        headers=headers,
    )
    assert created.status_code == 200
    person = created.json()
    person_id = person["person_id"]

    listing = client.get("/api/admin/persons", headers=headers)
    assert listing.status_code == 200
    assert [p["person_id"] for p in listing.json()["persons"]] == [person_id]

    # 未命名且无备注 → 画像框架为空
    empty_card = client.get(f"/api/admin/persons/{person_id}/card", headers=headers)
    assert empty_card.status_code == 200
    assert empty_card.json()["content"] == ""

    named = client.patch(
        f"/api/admin/persons/{person_id}",
        json={"display_name": "张三"},
        headers=headers,
    )
    assert named.status_code == 200
    assert named.json()["display_name"] == "张三"

    patched = client.patch(
        f"/api/admin/persons/{person_id}",
        json={"aliases": [{"participant_id": "wecom:single:zs", "channel": "wecom", "notes": ["工作伙伴"]}]},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["aliases"][0]["participant_id"] == "wecom:single:zs"
    assert patched.json()["aliases"][0]["notes"] == ["工作伙伴"]

    notes_saved = client.patch(
        f"/api/admin/persons/{person_id}",
        json={"notes": ["关系：好友", "工作日下午沟通顺畅"]},
        headers=headers,
    )
    assert notes_saved.status_code == 200
    assert notes_saved.json()["notes"] == ["关系：好友", "工作日下午沟通顺畅"]
    card = client.get(f"/api/admin/persons/{person_id}/card", headers=headers)
    assert "关系：好友" in card.json()["content"]
    assert "工作日下午沟通顺畅" in card.json()["content"]

    other = client.post(
        "/api/admin/persons",
        json={
            "display_name": "阿三",
            "notes": ["微信主号"],
            "aliases": [{"participant_id": "weixin:bot1", "notes": ["微信渠道"]}],
        },
        headers=headers,
    )
    other_id = other.json()["person_id"]

    merged = client.post(
        f"/api/admin/persons/{person_id}/merge",
        json={"other_person_id": other_id},
        headers=headers,
    )
    assert merged.status_code == 200
    assert {a["participant_id"] for a in merged.json()["aliases"]} == {
        "wecom:single:zs",
        "weixin:bot1",
    }
    # drop 人物的备注并入 keep
    assert set(merged.json()["notes"]) == {"关系：好友", "工作日下午沟通顺畅", "微信主号"}
    merged_card = client.get(f"/api/admin/persons/{person_id}/card", headers=headers)
    assert "微信渠道" in merged_card.json()["content"]

    deleted = client.delete(f"/api/admin/persons/{person_id}", headers=headers)
    assert deleted.status_code == 200
    gone = client.get(f"/api/admin/persons/{person_id}", headers=headers)
    assert gone.status_code == 404
    gone_card = client.get(f"/api/admin/persons/{person_id}/card", headers=headers)
    assert gone_card.status_code == 404


def test_person_admin_errors(tmp_path):
    client, _ = _client(tmp_path, persona=True)
    headers = {"Authorization": "Bearer secret"}

    missing = client.get("/api/admin/persons/p_nope", headers=headers)
    assert missing.status_code == 404

    invalid_merge = client.post(
        "/api/admin/persons/p_keep/merge",
        json={"other_person_id": "p_drop"},
        headers=headers,
    )
    assert invalid_merge.status_code == 400
