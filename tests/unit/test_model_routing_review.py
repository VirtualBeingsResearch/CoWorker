from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from coworker import application
from coworker.api import routes
from coworker.api.admin import router_module as admin
from coworker.core.config import Config
from coworker.core.exceptions import ProviderNotFoundError
from coworker.core.llm_errors import FailureCategory
from coworker.core.model_config import load_runtime_model_config, runtime_preference_revision
from coworker.core.model_routing import ModelRoutingConfig
from coworker.core.startup_intent import write_bootstrap_startup_intent
from coworker.core.tool_scope import ToolScope
from coworker.core.types import AgentState
from coworker.i18n import locale_context
from coworker.memory.short_term import ShortTermMemory
from coworker.tools.bubble_tools import _create_bubble_brain
from coworker.tools.code_tools import BackgroundJobStore
from coworker.tools.reasoning_tools import TaskStore
from coworker.tools.system_tools import GetContextTool
from tests.unit.test_admin_api import _client
from tests.unit.test_model_preference_startup import _brain, _confirm, _start
from tests.unit.test_model_preference_startup import installation as installation
from tests.unit.test_model_routing import make_brain, think


async def test_pending_preference_is_not_first_run_setup(installation, monkeypatch):
    brain, _ = await _start()
    monkeypatch.setattr(admin, "_brain", brain)
    monkeypatch.setattr(
        admin, "_require_admin_config_service",
        lambda: SimpleNamespace(snapshot=lambda: SimpleNamespace(config={}, secret_status={})),
    )
    result = await admin.bootstrap_status()
    assert result["required"] is False
    assert result["preference_pending"] is True


async def test_removed_saved_preference_enters_confirmation(installation):
    path = installation / "runtime.json"
    path.write_text(
        json.dumps({"preferred": {"provider": "removed", "model": "old-model"}}),
        encoding="utf-8",
    )
    brain, _ = await _start()
    assert brain.preference_pending
    snapshot = brain.model_config_snapshot()
    assert snapshot["preferred"] is None
    assert snapshot["preference"]["candidate"] == {"provider": "removed", "model": "old-model"}
    assert snapshot["preference"]["candidate_source"] == "saved_preference"
    assert all(provider.calls == 0 for provider in brain.router.providers.values())


async def test_bootstrap_api_choice_survives_a_removed_saved_preference(tmp_path, monkeypatch):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({"preferred": {"provider": "removed", "model": "old-model"}}), encoding="utf-8")
    test_client, config = _client(tmp_path, llm={"runtime_config_file": str(path)})
    admin._brain.active_provider = None
    old_revision = runtime_preference_revision(load_runtime_model_config(path), config.llm)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=test_client.app), base_url="http://test") as client:
        response = await client.post("/api/admin/bootstrap", headers={"Authorization": "Bearer secret"}, json={
            "provider_type": "openai", "model": "gpt-5.2", "api_key": "demo-key-not-valid",
        })
    assert response.status_code == 202
    intent = json.loads((tmp_path / "memory/startup_intent.json").read_text(encoding="utf-8"))
    assert intent["preference_revision"] == old_revision
    for name, value in {
        "ADMIN__CONFIG_FILE": str(tmp_path / "admin_config.json"),
        "LLM__RUNTIME_CONFIG_FILE": str(path), "LLM__PROVIDERS_FILE": "",
        "MEMORY__DB_PATH": str(tmp_path / "memory"),
    }.items():
        monkeypatch.setenv(name, value)
    restarted_config = application._load_config()
    from coworker.brain.brain import Brain

    brain = Brain(restarted_config.llm.default_provider, restarted_config.llm.default_model)
    application._register_providers(brain, restarted_config)
    await application._validate_model_runtime_config(brain, restarted_config)
    assert (brain.current_provider_name, brain.current_model) == ("openai", "gpt-5.2")
    assert brain.model_config_snapshot()["preference"]["selected_at"] == intent["selected_at"]
    assert not brain.preference_pending


@pytest.mark.parametrize("removed", [False, True])
@pytest.mark.parametrize("later_provider,later_model", [("zhipu", "glm-5.3-flash"), ("deepseek", "deepseek-v4-pro")])
async def test_bootstrap_receipt_overrides_old_selection_but_not_a_later_switch(installation, monkeypatch, removed, later_provider, later_model):
    monkeypatch.setenv("LLM__DEEPSEEK_API_KEY", "demo-key-not-valid")
    brain, config = await _start()
    await _confirm(brain, config)
    if removed:
        path = installation / "runtime.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["preferred"] = {"provider": "removed", "model": "old-model"}
        path.write_text(json.dumps(saved), encoding="utf-8")
    old = load_runtime_model_config(installation / "runtime.json")
    intent_path = write_bootstrap_startup_intent(
        config.memory.db_path, provider="deepseek", model="deepseek-v4-pro",
        preference_revision=runtime_preference_revision(old, config.llm),
    )
    receipt = json.loads(intent_path.read_text(encoding="utf-8"))
    policy = json.loads((installation / "runtime.json").read_text(encoding="utf-8"))
    policy["routing"]["failure_threshold"] = 5
    (installation / "runtime.json").write_text(json.dumps(policy), encoding="utf-8")
    # A restart interrupted after applying the selection may safely resume it.
    for _ in range(2):
        restarted, config = await _start()
        assert restarted.current_provider_name == config.llm.default_provider == "deepseek"
        assert restarted.model_config_snapshot()["preference"]["selected_at"] == receipt["selected_at"]
        assert restarted.routing_config.failure_threshold == 5
        assert intent_path.exists()
    await _confirm(restarted, config, later_provider, later_model)
    saved = (installation / "runtime.json").read_bytes()
    restarted, _ = await _start()
    assert (restarted.current_provider_name, restarted.current_model) == (later_provider, later_model)
    assert (installation / "runtime.json").read_bytes() == saved
    assert not intent_path.exists()


async def test_unlinked_legacy_bootstrap_marker_cannot_override_saved_preference(installation, monkeypatch):
    monkeypatch.setenv("LLM__DEEPSEEK_API_KEY", "demo-key-not-valid")
    brain, config = await _start()
    await _confirm(brain, config)
    path = write_bootstrap_startup_intent(config.memory.db_path, provider="deepseek", model="deepseek-v4-pro")
    restarted, _ = await _start()
    assert restarted.current_provider_name == "zhipu" and not path.exists()


async def test_unavailable_saved_model_and_stale_fallbacks_stay_repairable(installation, monkeypatch):
    path = installation / "runtime.json"
    path.write_text(json.dumps({
        "preferred": {"provider": "removed", "model": "old-model"},
        "fallbacks": ["removed/old-model", "backup/backup-model"],
    }), encoding="utf-8")
    brain, config = await _start()
    assert brain.preference_pending
    before = path.read_bytes()
    with pytest.raises(ProviderNotFoundError):
        await brain.switch_model("backup", "backup-model")
    assert path.read_bytes() == before and brain.preference_pending
    await brain.update_model_config(fallbacks=["backup/backup-model"])
    await brain.switch_model("backup", "backup-model")
    restarted, _ = await _start()
    assert restarted.current_provider_name == "backup" and not restarted.preference_pending


async def test_saved_model_without_tool_support_requires_confirmation(installation):
    path = installation / "runtime.json"
    path.write_text(json.dumps({"preferred": {"provider": "zhipu", "model": "no-tools"}}), encoding="utf-8")
    config = application._load_config()
    brain = _brain(config)
    brain.router.providers["zhipu"].declare_model_capabilities("no-tools", tools=False, vision=False, video=False)
    await application._validate_model_runtime_config(brain, config)
    assert brain.preference_pending
    assert brain.model_config_snapshot()["preference"]["candidate"]["model"] == "no-tools"


@pytest.mark.parametrize("locale", ["en", "zh-CN"])
@pytest.mark.parametrize("role", ["main", "bubble", "subconscious"])
async def test_context_reports_preference_and_real_execution(locale, role):
    from coworker.agent.subconscious import SubconsciousScheduler

    parent, primary, _, clock = make_brain(category=FailureCategory.RATE_LIMIT)
    if role == "bubble":
        brain = _create_bubble_brain(parent, "primary", "primary-model", inherit_policy=True)
    elif role == "subconscious":
        scheduler = SubconsciousScheduler.__new__(SubconsciousScheduler)
        scheduler._brain = parent
        brain = scheduler._create_brain()
    else:
        brain = parent
    tool = GetContextTool(parent, ShortTermMemory(), AgentState())
    if role != "main":
        tool = tool.fork(ToolScope(
            task_store=TaskStore(None), job_store=BackgroundJobStore(), inbox=None,
            brain=brain, short_term=ShortTermMemory(),
        ))
    with locale_context(locale):
        before = (await tool.execute()).content
        assert ("No completed model call" if locale == "en" else "尚无已完成的模型调用") in before
        assert (await think(brain)).provider == "backup"
        result = (await tool.execute()).content
        assert ("Preferred model: primary/primary-model" if locale == "en" else "首选模型：primary/primary-model") in result
        assert ("Last execution: backup/backup-model" if locale == "en" else "最近实际调用：backup/backup-model") in result
        clock.advance(30)
        primary.failure = None
        assert (await think(brain)).provider == "primary"
        recovered = (await tool.execute()).content
        assert ("Last execution: primary/primary-model" if locale == "en" else "最近实际调用：primary/primary-model") in recovered
    assert parent.current_provider_name == "primary"


def test_routing_rejects_initial_cooldown_larger_than_maximum():
    with pytest.raises(ValueError):
        ModelRoutingConfig(cooldown_seconds=1200)


@pytest.mark.parametrize("endpoint", ["/api/admin/model", "/model_config"])
@pytest.mark.parametrize("locale", ["en", "zh-CN"])
async def test_partial_cooldown_updates_validate_against_saved_limits(tmp_path, monkeypatch, endpoint, locale):
    brain, _, _, _ = make_brain(failure_threshold=1)
    config = Config(_env_file=None)
    config.admin.token = "demo-review-admin-token-not-valid"
    config.llm.runtime_config_file = str(tmp_path / "runtime.json")
    config.agent.logs_dir = str(tmp_path / "logs")
    monkeypatch.setattr(admin, "_brain", brain)
    monkeypatch.setattr(admin, "_config", config)
    monkeypatch.setattr(routes, "_brain", brain)
    monkeypatch.setattr(routes, "_model_config_path", tmp_path / "runtime.json")
    app = FastAPI()
    app.include_router(admin.router)
    app.include_router(routes.router)
    headers = {"Authorization": "Bearer demo-review-admin-token-not-valid"}
    with locale_context(locale):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            rejected = await client.patch(endpoint, headers=headers, json={"routing": {"cooldown_seconds": 1200}})
            assert rejected.status_code == 400
            assert ("Maximum cooldown" if locale == "en" else "最大冷却") in rejected.json()["detail"]
            assert not (tmp_path / "runtime.json").exists()
            invalid_pair = await client.patch(endpoint, headers=headers, json={"routing": {"cooldown_seconds": 1200, "max_cooldown_seconds": 900}})
            assert invalid_pair.status_code == 422
            configured = await client.patch(endpoint, headers=headers, json={"routing": {"cooldown_seconds": 1200, "max_cooldown_seconds": 1800}})
            assert configured.status_code == 200
            updated = await client.patch(endpoint, headers=headers, json={"routing": {"cooldown_seconds": 1500}})
            assert updated.status_code == 200
            assert updated.json()["routing"]["max_cooldown_seconds"] == 1800
            before = (tmp_path / "runtime.json").read_bytes()
            rejected = await client.patch(endpoint, headers=headers, json={"routing": {"max_cooldown_seconds": 1400}})
            assert rejected.status_code == 400
            assert (tmp_path / "runtime.json").read_bytes() == before
    assert (await think(brain)).provider == "backup"
    assert brain.router.snapshot()[0]["cooldown_seconds"] == 1500
