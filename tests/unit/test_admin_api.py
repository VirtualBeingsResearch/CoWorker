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
from coworker.channels.telegram import TelegramChannel, TelegramModule, TelegramSettings
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
from coworker.prompts.template import (
    DEFAULT_SYSTEM_PROMPT_TEMPLATE,
    SYSTEM_PROMPT_CONTENT_VARIABLES,
    SYSTEM_PROMPT_VARIABLES,
)
from coworker.skills.loader import SkillLoader


class _Identity:
    name = "Luna"


def _client(
    tmp_path,
    *,
    providers_file: str = "",
    llm: dict | None = None,
    api: dict | None = None,
    desktop_updates: dict | None = None,
    desktop_update_sync=None,
    alarm_manager=None,
    wecom: dict | None = None,
    weixin: dict | None = None,
    telegram: dict | None = None,
    channel_access: dict | None = None,
    channel_modules=None,
    relay_client=None,
    persona: bool = False,
    usage_stats=None,
    long_term=None,
    agent_config: dict | None = None,
):
    config = Config.model_validate(
        {
            "admin": {"token": "secret", "config_file": str(tmp_path / "admin_config.json")},
            "api": api or {},
            "llm": {
                "openai_api_key": "sk-original",
                "providers_file": providers_file,
                **(llm or {}),
            },
            "memory": {"db_path": str(tmp_path / "memory")},
            "agent": {"logs_dir": str(tmp_path / "logs"), **(agent_config or {})},
            "desktop_updates": desktop_updates or {},
            "wecom": wecom or {},
            "weixin": weixin or {},
            "telegram": telegram or {},
            "channel_access": channel_access or {},
        }
    )
    section_previews = [
        {
            "name": "IDENTITY",
            "variable": "IDENTITY",
            "content_variable": "IDENTITY_CONTENT",
            "full_text": "[IDENTITY]\nMy name is Luna.",
            "content": "My name is Luna.",
            "available": True,
            "lines": 2,
        }
    ]
    agent = SimpleNamespace(
        _identity=_Identity(),
        _snapshot_path=tmp_path / "short_term_snapshot.json",
        _short_term=ShortTermMemory(),
        state=SimpleNamespace(current_provider="", current_model=""),
        request_restart=lambda reason="normal": None,
        resume_from_rest=MagicMock(return_value=True),
        current_system_prompt=MagicMock(return_value="[IDENTITY]\nMy name is Luna.\n"),
        current_system_prompt_sections=MagicMock(return_value=section_previews),
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
        switch_model=AsyncMock(),
        model_config_snapshot=lambda: _brain_snapshot,
        update_model_config=AsyncMock(return_value=_brain_snapshot),
        model_catalog_snapshot=lambda: {"providers": []},
        refresh_model_catalog=AsyncMock(return_value={"providers": []}),
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


def test_admin_communication_token_copy_endpoint_returns_effective_token(tmp_path):
    client, _ = _client(tmp_path, api={"communication_token": "desktop-secret"})
    headers = {"Authorization": "Bearer secret"}

    dedicated = client.get("/api/admin/communication-token", headers=headers)
    fallback_client, _ = _client(tmp_path / "fallback")

    fallback = fallback_client.get(
        "/api/admin/communication-token",
        headers={"Authorization": "Bearer secret"},
    )

    assert dedicated.status_code == 200
    assert dedicated.json() == {
        "communication_token": "desktop-secret",
        "source": "api.communication_token",
        "explicit": True,
    }
    assert fallback.status_code == 200
    assert fallback.json() == {
        "communication_token": "secret",
        "source": "admin.token",
        "explicit": False,
    }


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
    usage_stats.report.assert_called_once_with(model_prices=[])


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
        model_prices=[],
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
        model_prices=[],
    )


def test_admin_usage_prices_report_with_current_llm_config(tmp_path):
    usage_stats = SimpleNamespace(report=MagicMock(return_value={"today": {}}))
    client, config = _client(
        tmp_path,
        usage_stats=usage_stats,
        llm={
            "model_prices": [
                {
                    "provider": "openai",
                    "model": "gpt-5.2",
                    "currency": "USD",
                    "input_per_million": 1.75,
                    "output_per_million": 14,
                }
            ]
        },
    )

    response = client.get(
        "/api/admin/usage",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    usage_stats.report.assert_called_once_with(model_prices=config.llm.model_prices)


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


def test_admin_can_delete_emergency_backup_with_confirmation(tmp_path):
    client, _ = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}
    filename = "emergency_backup_20260823_010203.json"
    backup = tmp_path / filename
    backup.write_text('{"primary": []}', encoding="utf-8")

    assert client.delete(f"/api/admin/backups/{filename}").status_code == 401
    rejected = client.request(
        "DELETE",
        f"/api/admin/backups/{filename}",
        headers=headers,
        json={"confirm_name": "wrong"},
    )
    assert rejected.status_code == 400
    assert backup.is_file()

    deleted = client.request(
        "DELETE",
        f"/api/admin/backups/{filename}",
        headers=headers,
        json={"confirm_name": "Luna"},
    )

    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "filename": filename}
    assert not backup.exists()
    audit = (tmp_path / "logs" / "admin_audit.jsonl").read_text(encoding="utf-8")
    assert '"action": "backup.delete"' in audit
    assert f'"target": "{filename}"' in audit


def test_admin_backup_delete_rejects_invalid_or_missing_file(tmp_path):
    client, _ = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}
    payload = {"confirm_name": "Luna"}
    unrelated = tmp_path / "notes.json"
    unrelated.write_text("{}", encoding="utf-8")

    invalid = client.request(
        "DELETE",
        "/api/admin/backups/notes.json",
        headers=headers,
        json=payload,
    )
    missing = client.request(
        "DELETE",
        "/api/admin/backups/emergency_backup_20990101_000000.json",
        headers=headers,
        json=payload,
    )

    assert invalid.status_code == 400
    assert unrelated.is_file()
    assert missing.status_code == 404


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
        "active_template": DEFAULT_SYSTEM_PROMPT_TEMPLATE,
        "desired_template": DEFAULT_SYSTEM_PROMPT_TEMPLATE,
        "inherited_template": DEFAULT_SYSTEM_PROMPT_TEMPLATE,
        "default_template": DEFAULT_SYSTEM_PROMPT_TEMPLATE,
        "variables": list(SYSTEM_PROMPT_VARIABLES),
        "content_variables": list(SYSTEM_PROMPT_CONTENT_VARIABLES),
        "section_previews": [
            {
                "name": "IDENTITY",
                "variable": "IDENTITY",
                "content_variable": "IDENTITY_CONTENT",
                "full_text": "[IDENTITY]\nMy name is Luna.",
                "content": "My name is Luna.",
                "available": True,
                "lines": 2,
            }
        ],
        "overridden": False,
        "prompt_pending_restart": False,
    }
    admin._agent.current_system_prompt.assert_called_once_with()
    admin._agent.current_system_prompt_sections.assert_called_once_with()


def test_system_prompt_template_patch_waits_for_restart_and_keeps_active_prompt(tmp_path):
    client, config = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}
    custom = "{{IDENTITY}}\n\n[PROJECT]\nUse the release checklist."

    updated = client.patch(
        "/api/admin/config",
        headers=headers,
        json={"changes": {"agent": {"system_prompt_template": custom}}},
    )
    snapshot = client.get("/api/admin/system-prompt", headers=headers)

    assert updated.status_code == 200
    assert updated.json()["applied_now"] == []
    assert updated.json()["requires_restart"] == ["agent.system_prompt_template"]
    assert config.agent.system_prompt_template == ""
    assert snapshot.json()["content"] == "[IDENTITY]\nMy name is Luna.\n"
    assert snapshot.json()["active_template"] == DEFAULT_SYSTEM_PROMPT_TEMPLATE
    assert snapshot.json()["desired_template"] == custom
    assert snapshot.json()["overridden"] is True
    assert snapshot.json()["prompt_pending_restart"] is True
    assert json.loads((tmp_path / "admin_config.json").read_text(encoding="utf-8"))[
        "agent"
    ]["system_prompt_template"] == custom


@pytest.mark.parametrize(
    "template",
    [
        "{{UNKNOWN}}",
        "{{IDENTITY}}\n\n{{IDENTITY_CONTENT}}",
    ],
)
def test_system_prompt_template_validation_failure_does_not_write_override(
    tmp_path,
    template,
):
    client, _ = _client(tmp_path)

    response = client.patch(
        "/api/admin/config",
        headers={"Authorization": "Bearer secret"},
        json={"changes": {"agent": {"system_prompt_template": template}}},
    )

    assert response.status_code == 422
    assert not (tmp_path / "admin_config.json").exists()


def test_system_prompt_template_can_restore_builtin_then_inherited_value(tmp_path):
    inherited = "{{IDENTITY}}\n\n[ENVIRONMENT_RULE]\nInherited text"
    client, _ = _client(
        tmp_path,
        agent_config={"system_prompt_template": inherited},
    )
    headers = {"Authorization": "Bearer secret"}

    builtin = client.patch(
        "/api/admin/config",
        headers=headers,
        json={"changes": {"agent": {"system_prompt_template": ""}}},
    )
    builtin_snapshot = client.get("/api/admin/system-prompt", headers=headers).json()
    restored = client.patch(
        "/api/admin/config",
        headers=headers,
        json={"clear_overrides": ["agent.system_prompt_template"]},
    )
    inherited_snapshot = client.get("/api/admin/system-prompt", headers=headers).json()

    assert builtin.status_code == 200
    assert builtin_snapshot["desired_template"] == DEFAULT_SYSTEM_PROMPT_TEMPLATE
    assert builtin_snapshot["inherited_template"] == inherited
    assert builtin_snapshot["overridden"] is True
    assert restored.status_code == 200
    assert restored.json()["pending_restart"] is False
    assert inherited_snapshot["desired_template"] == inherited
    assert inherited_snapshot["overridden"] is False


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


def test_config_patch_hot_applies_model_prices_without_rebuilding_provider(tmp_path):
    client, config = _client(tmp_path)
    price = {
        "provider": "openai",
        "model": "gpt-5.2",
        "currency": "usd",
        "input_per_million": 1.75,
        "output_per_million": 14,
        "cached_input_per_million": 0.175,
    }

    response = client.patch(
        "/api/admin/config",
        headers={"Authorization": "Bearer secret"},
        json={"changes": {"llm": {"model_prices": [price]}}},
    )

    assert response.status_code == 200
    assert response.json()["applied_now"] == ["llm.model_prices"]
    assert response.json()["requires_restart"] == []
    assert response.json()["pending_restart"] is False
    assert config.llm.model_prices[0].currency == "USD"
    admin._brain.upsert_provider.assert_not_awaited()
    saved = json.loads((tmp_path / "admin_config.json").read_text(encoding="utf-8"))
    assert saved["llm"]["model_prices"][0]["currency"] == "USD"


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


def test_config_patch_hot_applies_communication_token(tmp_path):
    client, config = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}

    snapshot = client.get("/api/admin/config", headers=headers).json()
    assert "api.communication_token" in snapshot["hot_reloadable"]

    import coworker.api.routes as routes_mod

    routes_mod._communication_token = "old-token"
    routes_mod._communication_token_explicit = False

    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={"changes": {}, "secrets": {"api.communication_token": "desktop-new"}},
    )

    assert response.status_code == 200
    assert response.json()["applied_now"] == ["api.communication_token"]
    assert response.json()["requires_restart"] == []
    assert response.json()["pending_restart"] is False
    assert routes_mod._communication_token == "desktop-new"
    assert routes_mod._communication_token_explicit is True
    assert config.api.communication_token == "desktop-new"

    cleared = client.patch(
        "/api/admin/config",
        headers=headers,
        json={"changes": {}, "secrets": {"api.communication_token": ""}},
    )

    assert cleared.status_code == 200
    assert cleared.json()["applied_now"] == ["api.communication_token"]
    assert cleared.json()["requires_restart"] == []
    assert cleared.json()["pending_restart"] is False
    assert routes_mod._communication_token == "secret"
    assert routes_mod._communication_token_explicit is False
    assert config.api.communication_token == ""


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


def test_config_patch_hot_updates_memory_relevance_threshold(tmp_path):
    client, config = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}

    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={
            "changes": {"memory": {"auto_recall_relevance_threshold": 0.8}},
            "secrets": {},
        },
    )

    assert response.status_code == 200
    assert response.json()["applied_now"] == [
        "memory.auto_recall_relevance_threshold"
    ]
    assert config.memory.auto_recall_relevance_threshold == 0.8


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
        wecom={
            "bots": {
                "main": {"enabled": True, "bot_id": "old", "secret": "existing"},
            }
        },
        channel_modules=modules,
    )
    headers = {"Authorization": "Bearer secret"}

    body = client.get("/api/admin/config", headers=headers).json()
    assert "wecom" in body["hot_reloadable"]
    assert body["config"]["wecom"]["bots"]["main"]["secret"] == ""
    assert body["secret_status"]["wecom.bots.main.secret"]["last4"] == "ting"

    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={
            "changes": {
                "wecom": {
                    "bots": {
                        "main": {
                            "enabled": True,
                            "bot_id": "new",
                            "secret": "",
                            "ws_url": "wss://wecom.example/ws",
                        },
                        "work": {
                            "enabled": True,
                            "bot_id": "work-bot",
                            "secret": "",
                            "ws_url": "wss://work.example/ws",
                        },
                    }
                }
            },
            "secrets": {"wecom.bots.work.secret": "work-secret"},
        },
    )

    assert response.status_code == 200
    assert response.json()["applied_now"] == ["wecom"]
    assert response.json()["requires_restart"] == []
    assert response.json()["pending_restart"] is False
    assert config.wecom.bots["main"].bot_id == "new"
    assert config.wecom.bots["main"].secret == "existing"
    assert config.wecom.bots["work"].secret == "work-secret"
    applied = runner.reconfigure.await_args.args[0]
    assert applied.bots["main"].ws_url == "wss://wecom.example/ws"
    assert applied.bots["main"].secret == "existing"
    assert applied.bots["work"].secret == "work-secret"
    saved = json.loads((tmp_path / "admin_config.json").read_text(encoding="utf-8"))
    assert saved["wecom"]["bots"]["main"]["bot_id"] == "new"
    assert "secret" not in saved["wecom"]["bots"]["main"]
    assert saved["wecom"]["bots"]["work"]["secret"] == "work-secret"

    runner.reconfigure.reset_mock()
    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={
            "changes": {
                "wecom": {
                    "bots": {
                        "main": {
                            "enabled": True,
                            "bot_id": "new",
                            "secret": "",
                            "ws_url": "wss://wecom.example/ws",
                        }
                    }
                }
            },
            "secrets": {},
        },
    )

    assert response.status_code == 200
    assert set(config.wecom.bots) == {"main"}
    runner.reconfigure.assert_awaited_once()
    assert set(runner.reconfigure.await_args.args[0].bots) == {"main"}
    saved = json.loads((tmp_path / "admin_config.json").read_text(encoding="utf-8"))
    assert set(saved["wecom"]["bots"]) == {"main"}


def test_wecom_add_bot_keeps_legacy_default_instance(tmp_path):
    # 旧版扁平配置（WECOM__BOT_ID 等）折叠成 default 实例后，在管理端新增
    # 第二个 Bot 必须保留原 default 实例；否则原 Bot 停止接收消息。
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
        wecom={
            "enabled": True,
            "bot_id": "legacy-bot",
            "secret": "legacy-secret",
            "ws_url": "wss://legacy.example/ws",
        },
        channel_modules=modules,
    )
    headers = {"Authorization": "Bearer secret"}

    body = client.get("/api/admin/config", headers=headers).json()
    assert set(body["config"]["wecom"]["bots"]) == {"default"}
    assert body["config"]["wecom"]["bots"]["default"]["bot_id"] == "legacy-bot"
    assert body["secret_status"]["wecom.bots.default.secret"]["last4"] == "cret"

    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={
            "changes": {
                "wecom": {
                    "bots": {
                        "default": {
                            "enabled": True,
                            "bot_id": "legacy-bot",
                            "secret": "",
                            "ws_url": "wss://legacy.example/ws",
                        },
                        "work": {
                            "enabled": True,
                            "bot_id": "work-bot",
                            "secret": "",
                            "ws_url": "",
                        },
                    }
                }
            },
            "secrets": {"wecom.bots.work.secret": "work-secret"},
        },
    )

    assert response.status_code == 200
    assert response.json()["applied_now"] == ["wecom"]
    assert set(config.wecom.bots) == {"default", "work"}
    assert config.wecom.bots["default"].bot_id == "legacy-bot"
    assert config.wecom.bots["default"].secret == "legacy-secret"
    assert config.wecom.bots["work"].secret == "work-secret"
    applied = runner.reconfigure.await_args.args[0]
    assert set(applied.bots) == {"default", "work"}
    assert applied.bots["default"].bot_id == "legacy-bot"
    assert applied.bots["work"].bot_id == "work-bot"

    saved = json.loads((tmp_path / "admin_config.json").read_text(encoding="utf-8"))
    assert set(saved["wecom"]["bots"]) == {"default", "work"}

    # 重启后（.env 扁平 + admin_config.json）两个实例仍然都在
    restarted = apply_admin_config_file(
        Config.model_validate(
            {
                "admin": {"token": "secret", "config_file": str(tmp_path / "admin_config.json")},
                "wecom": {
                    "enabled": True,
                    "bot_id": "legacy-bot",
                    "secret": "legacy-secret",
                    "ws_url": "wss://legacy.example/ws",
                },
            }
        )
    )
    assert set(restarted.wecom.bots) == {"default", "work"}
    assert restarted.wecom.bots["default"].secret == "legacy-secret"
    assert restarted.wecom.bots["work"].secret == "work-secret"


def test_wecom_add_bot_preserves_legacy_flat_secret(tmp_path):
    # 旧版管理控制台把企业微信配置写成 wecom 层扁平字段（wecom.secret 等），
    # 而不是 wecom.bots.*。这类部署在新增第二个 Bot 时，扁平 secret 是 default
    # 实例 secret 的唯一副本，必须保留。
    admin_file = tmp_path / "admin_config.json"
    admin_file.write_text(
        json.dumps(
            {
                "wecom": {
                    "enabled": True,
                    "bot_id": "legacy-bot",
                    "secret": "legacy-secret",
                    "ws_url": "wss://legacy.example/ws",
                }
            }
        ),
        encoding="utf-8",
    )
    runner = SimpleNamespace(reconfigure=AsyncMock())
    modules = ChannelModuleRegistry()
    modules.register(
        WeComModule(
            channel=WeComChannel(runner),
            runtime=runner,
            settings=WeComSettings(runner),
        )
    )
    # .env 里没有任何 wecom 配置（旧配置全部来自 admin_config.json）
    client, config = _client(
        tmp_path,
        wecom={},
        channel_modules=modules,
    )
    headers = {"Authorization": "Bearer secret"}

    body = client.get("/api/admin/config", headers=headers).json()
    assert set(body["config"]["wecom"]["bots"]) == {"default"}
    assert body["secret_status"]["wecom.bots.default.secret"]["last4"] == "cret"

    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={
            "changes": {
                "wecom": {
                    "bots": {
                        "default": {
                            "enabled": True,
                            "bot_id": "legacy-bot",
                            "secret": "",
                            "ws_url": "wss://legacy.example/ws",
                        },
                        "work": {
                            "enabled": True,
                            "bot_id": "work-bot",
                            "secret": "",
                            "ws_url": "",
                        },
                    }
                }
            },
            "secrets": {"wecom.bots.work.secret": "work-secret"},
        },
    )

    assert response.status_code == 200
    assert response.json()["applied_now"] == ["wecom"]
    # default 的 secret 从扁平字段并入，没有丢失
    assert config.wecom.bots["default"].secret == "legacy-secret"
    assert config.wecom.bots["work"].secret == "work-secret"
    applied = runner.reconfigure.await_args.args[0]
    assert applied.bots["default"].secret == "legacy-secret"
    assert applied.bots["work"].secret == "work-secret"

    # 持久化文件保留扁平 secret（default 的唯一副本），重启后仍可恢复
    saved = json.loads((tmp_path / "admin_config.json").read_text(encoding="utf-8"))
    assert saved["wecom"]["secret"] == "legacy-secret"
    restarted = apply_admin_config_file(
        Config.model_validate(
            {
                "admin": {"token": "secret", "config_file": str(tmp_path / "admin_config.json")},
                "wecom": {},
            }
        )
    )
    assert set(restarted.wecom.bots) == {"default", "work"}
    assert restarted.wecom.bots["default"].secret == "legacy-secret"
    assert restarted.wecom.bots["work"].secret == "work-secret"


def test_telegram_config_hot_applies_multiple_bots_and_masks_tokens(tmp_path):
    runner = SimpleNamespace(
        name="telegram",
        start=AsyncMock(),
        stop=AsyncMock(),
        reconfigure=AsyncMock(),
        resolve_participant=lambda participant: None,
        set_inbound_handler=lambda handler: None,
        set_access_controller=lambda access: None,
        contacts=lambda: [],
        activity_for=lambda participant: (None, None),
    )
    modules = ChannelModuleRegistry()
    modules.register(
        TelegramModule(
            channel=TelegramChannel(runner),
            runtime=runner,
            settings=TelegramSettings(runner),
        )
    )
    client, config = _client(
        tmp_path,
        telegram={
            "bots": {
                "main": {
                    "display_name": "Main",
                    "bot_token": "main-existing",
                }
            }
        },
        channel_modules=modules,
    )
    headers = {"Authorization": "Bearer secret"}

    body = client.get("/api/admin/config", headers=headers).json()
    assert "telegram" in body["hot_reloadable"]
    assert body["config"]["telegram"]["bots"]["main"]["bot_token"] == ""
    assert body["secret_status"]["telegram.bots.main.bot_token"]["last4"] == "ting"

    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={
            "changes": {
                "telegram": {
                    "bots": {
                        "main": {
                            "enabled": True,
                            "display_name": "Primary",
                            "bot_token": "must-not-be-accepted-as-a-plain-field",
                            "api_base_url": "https://api.telegram.org",
                            "local_mode": False,
                            "poll_timeout_seconds": 30,
                        },
                        "work": {
                            "enabled": True,
                            "display_name": "Work",
                            "bot_token": "",
                            "api_base_url": "https://telegram.example/api",
                            "local_mode": True,
                            "poll_timeout_seconds": 20,
                        },
                    }
                }
            },
            "secrets": {"telegram.bots.work.bot_token": "work-secret"},
        },
    )

    assert response.status_code == 200
    assert response.json()["applied_now"] == ["telegram"]
    assert config.telegram.bots["main"].bot_token == "main-existing"
    assert config.telegram.bots["work"].bot_token == "work-secret"
    applied = runner.reconfigure.await_args.args[0]
    assert applied.bots["main"].display_name == "Primary"
    assert applied.bots["main"].bot_token == "main-existing"
    assert applied.bots["work"].api_base_url == "https://telegram.example/api"
    saved = json.loads((tmp_path / "admin_config.json").read_text(encoding="utf-8"))
    assert saved["telegram"]["bots"]["main"]["display_name"] == "Primary"
    assert "bot_token" not in saved["telegram"]["bots"]["main"]
    assert saved["telegram"]["bots"]["work"]["bot_token"] == "work-secret"

    runner.reconfigure.reset_mock()
    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={
            "changes": {
                "telegram": {
                    "bots": {
                        "main": {
                            "enabled": True,
                            "display_name": "Primary",
                            "bot_token": "",
                            "api_base_url": "https://api.telegram.org",
                            "local_mode": False,
                            "poll_timeout_seconds": 30,
                        }
                    }
                }
            },
            "secrets": {},
        },
    )

    assert response.status_code == 200
    assert set(config.telegram.bots) == {"main"}
    runner.reconfigure.assert_awaited_once()
    assert set(runner.reconfigure.await_args.args[0].bots) == {"main"}
    saved = json.loads((tmp_path / "admin_config.json").read_text(encoding="utf-8"))
    assert set(saved["telegram"]["bots"]) == {"main"}


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


def test_mem0_llm_config_rejects_failed_runtime_reconfigure(tmp_path):
    long_term = SimpleNamespace(
        reconfigure=AsyncMock(side_effect=RuntimeError("factory failed"))
    )
    client, config = _client(tmp_path, long_term=long_term)
    headers = {"Authorization": "Bearer secret"}

    response = client.patch(
        "/api/admin/config",
        headers=headers,
        json={
            "changes": {
                "memory": {
                    "mem0_llm_provider": "qwen",
                    "mem0_llm_model": "qwen3.6-flash",
                }
            },
            "secrets": {},
        },
    )

    assert response.status_code == 400
    assert "factory failed" in str(response.json()["detail"])
    assert config.memory.mem0_llm_provider == ""
    assert config.memory.mem0_llm_model == ""


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


def test_model_switch_response_preserves_mem0_view(tmp_path):
    client, _ = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}

    response = client.post(
        "/api/admin/model/switch",
        headers=headers,
        json={"provider": "openai", "model_id": "gpt-5.2"},
    )

    assert response.status_code == 200
    assert response.json()["mem0"] == {
        "provider": "",
        "model": "",
        "thinking": False,
    }
    assert response.json()["active_changed"] is False


def test_admin_model_switch_leaves_runtime_notice_pending_for_agent_loop(tmp_path):
    client, _ = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}
    admin._agent.state.current_provider = "openai"
    admin._agent.state.current_model = "gpt-5.2"
    original_snapshot = admin._brain.model_config_snapshot()

    async def switch(provider: str, model: str) -> None:
        admin._brain.current_provider_name = provider
        admin._brain.current_model = model

    admin._brain.switch_model = AsyncMock(side_effect=switch)
    admin._brain.model_config_snapshot = lambda: {
        **original_snapshot,
        "active": {
            "provider": admin._brain.current_provider_name,
            "model": admin._brain.current_model,
        },
    }

    response = client.post(
        "/api/admin/model/switch",
        headers=headers,
        json={"provider": "openai", "model_id": "gpt-5.4"},
    )

    assert response.status_code == 200
    assert response.json()["active_changed"] is True
    assert response.json()["active"] == {"provider": "openai", "model": "gpt-5.4"}
    # AgentLoop compares these acknowledged values with Brain on its next cycle
    # and injects the localized model-switch notice before the next inference.
    assert admin._agent.state.current_provider == "openai"
    assert admin._agent.state.current_model == "gpt-5.2"


def test_model_catalog_lists_registered_providers(tmp_path):
    client, _ = _client(tmp_path)
    admin._brain.model_catalog_snapshot = lambda: {
        "providers": [
            {
                "name": "openai",
                "type": "openai",
                "static_models": ["gpt-5.2"],
                "remote_models": [],
                "models": ["gpt-5.2"],
                "error": None,
                "fetched_at": None,
            }
        ]
    }
    headers = {"Authorization": "Bearer secret"}

    response = client.get("/api/admin/model/catalog", headers=headers)

    assert response.status_code == 200
    provider = response.json()["providers"][0]
    assert provider["name"] == "openai"
    assert "gpt-5.2" in provider["static_models"]


def test_model_catalog_refresh_calls_provider_and_returns_catalog(tmp_path):
    client, _ = _client(tmp_path)
    admin._brain.refresh_model_catalog = AsyncMock(
        return_value={
            "providers": [
                {
                    "name": "openai",
                    "type": "openai",
                    "static_models": ["gpt-5.2"],
                    "remote_models": ["gpt-remote"],
                    "models": ["gpt-5.2", "gpt-remote"],
                    "error": None,
                    "fetched_at": 1.0,
                }
            ]
        }
    )
    headers = {"Authorization": "Bearer secret"}

    response = client.post(
        "/api/admin/model/catalog/refresh",
        headers=headers,
        json={"provider": "openai"},
    )

    assert response.status_code == 202
    admin._brain.refresh_model_catalog.assert_awaited_once_with("openai")
    provider = response.json()["providers"][0]
    assert provider["remote_models"] == ["gpt-remote"]
    assert "gpt-remote" in provider["models"]


def test_model_discover_uses_temporary_credentials(tmp_path, monkeypatch):
    from coworker.brain.openai_provider import OpenAIProvider

    async def fake_fetch(self):
        return self.mark_remote_models(["discovered-model"])

    monkeypatch.setattr(OpenAIProvider, "fetch_models", fake_fetch)
    client, _ = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}

    response = client.post(
        "/api/admin/model/discover",
        headers=headers,
        json={"provider_type": "openai", "api_key": "sk-temp", "base_url": ""},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "openai"
    assert body["remote_models"] == ["discovered-model"]
    assert "discovered-model" in body["models"]


def test_model_discover_rejects_unknown_provider(tmp_path):
    client, _ = _client(tmp_path)
    headers = {"Authorization": "Bearer secret"}

    response = client.post(
        "/api/admin/model/discover",
        headers=headers,
        json={"provider_type": "nope", "api_key": "sk-temp"},
    )

    assert response.status_code == 400
    assert "nope" in response.json()["detail"]


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
        "tg:*",
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
    monkeypatch.setattr(admin, "_server_timezone", lambda: "Asia/Shanghai")
    headers = {"Authorization": "Bearer secret"}

    status = client.get("/api/admin/bootstrap", headers=headers)
    assert status.status_code == 200
    assert status.json()["required"] is True
    assert status.json()["server_timezone"] == "Asia/Shanghai"
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
                    "cors_origins": ["https://desktop.example"],
                },
                "relay": {
                    "enabled": False,
                    "url": "https://relay.example.test",
                    "instance_id": "cw_abcdefgh",
                    "auth_epoch": 2,
                },
                "channel_access": {
                    "wecom": {"inbound_allow": ["wecom:first-run:single:*"]}
                },
                "wecom": {
                    "bots": {
                        "first-run": {
                            "enabled": True,
                            "bot_id": "bot-first-run",
                            "secret": "",
                            "ws_url": "wss://wecom.example.test/ws",
                        }
                    }
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
                "wecom.bots.first-run.secret": "wecom-first-run",
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
    assert saved["channel_access"]["wecom"]["inbound_allow"] == ["wecom:first-run:single:*"]
    assert saved["wecom"]["bots"]["first-run"]["secret"] == "wecom-first-run"
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

    response = client.get(
        "/api/admin/bubbles?bubble_id=bbl_260716120000", headers=headers
    )
    assert [item["id"] for item in response.json()["bubbles"]] == [
        "bbl_260716120000"
    ]

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
    response = client.get(
        "/api/admin/subconscious?bubble_id=bbl_260716120000_audit",
        headers=headers,
    )
    assert [item["log_id"] for item in response.json()["bubbles"]] == [
        "bbl_260716120000_audit"
    ]
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
                        "data": "c2VjcmV0LWltYWdlLWJ5dGVz",
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
        {
            "type": "image",
            "media_type": "image/png",
            "filename": "example.png",
            "preview_url": "/api/admin/memory/short-term/messages/0/content/1",
        },
    ]
    assert "c2VjcmV0LWltYWdlLWJ5dGVz" not in response.text

    preview = client.get(message["content"][1]["preview_url"], headers={"Authorization": "Bearer secret"})
    assert preview.status_code == 200
    assert preview.content == b"secret-image-bytes"
    assert preview.headers["content-type"] == "image/png"
    assert preview.headers["cache-control"] == "private, no-store"
    assert preview.headers["x-content-type-options"] == "nosniff"
    assert client.get(message["content"][1]["preview_url"]).status_code == 401


def test_short_term_tool_image_result_exposes_authenticated_preview(tmp_path):
    client, config = _client(tmp_path)
    short_term = ShortTermMemory(max_tokens=1_000)
    short_term.primary.extend(
        [
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "call_view_image",
                        "type": "function",
                        "function": {"name": "view_image", "arguments": "{}"},
                    }
                ],
            ),
            Message(
                role="tool",
                tool_call_id="call_view_image",
                content=[
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": "/9g=",
                        },
                        "_filename": "screen.jpg",
                    },
                    {"type": "text", "text": "图片已加载"},
                ],
            ),
        ]
    )
    agent = SimpleNamespace(
        _identity=_Identity(),
        _short_term=short_term,
        state=SimpleNamespace(last_main_response_usage=None),
    )
    brain = SimpleNamespace(
        active_provider=None,
        current_provider_name="opencode-go",
        current_model="kimi-k3",
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
    response = client.get("/api/admin/memory/short-term/messages", headers=headers)

    assert response.status_code == 200
    messages = response.json()["messages"]
    assert len(messages) == 1
    result = messages[0]["tool_calls"][0]["result"]
    assert result[0]["preview_url"] == (
        "/api/admin/memory/short-term/messages/1/content/0"
    )
    preview = client.get(result[0]["preview_url"], headers=headers)
    assert preview.status_code == 200
    assert preview.content == b"\xff\xd8"
    assert client.get(
        "/api/admin/memory/short-term/messages/1/content/1", headers=headers
    ).status_code == 404


def test_short_term_messages_tail_is_lightweight_and_matches_full_snapshot(tmp_path):
    client, config = _client(tmp_path)
    short_term = ShortTermMemory(max_tokens=1_000)
    short_term.primary.append(Message(role="user", content="first"))
    short_term.primary.append(
        Message(
            role="assistant",
            content="second",
            usage={"input_tokens": 100, "output_tokens": 20, "cached_tokens": 60},
            duration_ms=1_250,
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

    headers = {"Authorization": "Bearer secret"}
    full = client.get("/api/admin/memory/short-term", headers=headers).json()
    tail = client.get("/api/admin/memory/short-term/messages", headers=headers)

    assert tail.status_code == 200
    body = tail.json()
    assert set(body) == {"messages"}
    assert body["messages"] == full["messages"]
    assert body["messages"][1]["usage"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cached_tokens": 60,
    }
    assert body["messages"][1]["duration_ms"] == 1_250


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


def test_admin_interaction_page_links_legacy_bubble_tool_pairs(tmp_path):
    client, config = _client(tmp_path)
    logs_dir = Path(config.agent.logs_dir)
    logs_dir.mkdir(parents=True)
    archived = [
        {
            "type": "message_in",
            "seq": 0,
            "ts": "2026-07-01T09:00:00",
            "content": "开始",
        },
        {
            "type": "tool_call",
            "seq": 1,
            "ts": "2026-07-01T09:01:00",
            "id": "call-bubble",
            "name": "bubble_spawn",
            "arguments": {"goal": "核对发布"},
        },
    ]
    active = [
        {
            "type": "tool_result",
            "seq": 2,
            "ts": "2026-07-01T09:02:00",
            "id": "call-bubble",
            "name": "bubble_spawn",
            "content": "已创建泡泡 bbl_260701090200，正在后台处理。",
        },
        {
            "type": "subconscious_done",
            "seq": 3,
            "ts": "2026-07-01T09:03:00",
            "mode": "audit",
            "bubble_id": "bbl_260701090300",
            "result": "完成",
        },
    ]
    (logs_dir / "interactions-000001.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in archived) + "\n",
        encoding="utf-8",
    )
    (logs_dir / "interactions.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in active) + "\n",
        encoding="utf-8",
    )

    response = client.get(
        "/api/admin/interactions?limit=100&seq_end=3",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert [item["seq"] for item in payload["events"]] == [3, 2, 1, 0]
    assert payload["events"][2]["bubble"] == {
        "id": "bbl_260701090200",
        "bubble_id": "bbl_260701090200",
        "scope": "bubbles",
    }
    assert payload["events"][1]["bubble"] == payload["events"][2]["bubble"]
    assert payload["events"][0]["bubble"] == {
        "id": "bbl_260701090300_audit",
        "bubble_id": "bbl_260701090300",
        "scope": "subconscious",
    }


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
    assert first.json()["time_range"] == {
        "start_time": "2026-07-01T09:00:00",
        "end_time": "2026-07-01T09:59:59.999999",
    }
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


def test_person_model_api_token_issue_and_revoke(tmp_path):
    client, _ = _client(tmp_path, persona=True)
    auth = {"Authorization": "Bearer secret"}
    person = client.post(
        "/api/admin/persons", headers=auth, json={"display_name": "Alice"}
    ).json()

    issued = client.post(
        f"/api/admin/persons/{person['person_id']}/model-api-token",
        headers=auth,
        json={},
    )
    assert issued.status_code == 201
    body = issued.json()
    assert body["participant_id"] == "api:alice"
    assert body["token"].startswith("sk-")

    detail = client.get(
        f"/api/admin/persons/{person['person_id']}", headers=auth
    ).json()
    channels = [alias["channel"] for alias in detail["aliases"]]
    assert "model-api" in channels

    # A second token for the same person gets a distinct address.
    second = client.post(
        f"/api/admin/persons/{person['person_id']}/model-api-token",
        headers=auth,
        json={},
    ).json()
    assert second["participant_id"] == "api:alice-2"

    revoked = client.delete(
        f"/api/admin/persons/{person['person_id']}/model-api-token/{body['key']}",
        headers=auth,
    )
    assert revoked.status_code == 200
    detail = client.get(
        f"/api/admin/persons/{person['person_id']}", headers=auth
    ).json()
    participants = [alias["participant_id"] for alias in detail["aliases"]]
    assert "api:alice" not in participants
    assert "api:alice-2" in participants


def test_person_model_api_token_requires_existing_person(tmp_path):
    client, _ = _client(tmp_path, persona=True)
    auth = {"Authorization": "Bearer secret"}
    missing = client.post(
        "/api/admin/persons/p_nonexistent/model-api-token",
        headers=auth,
        json={},
    )
    assert missing.status_code == 404
