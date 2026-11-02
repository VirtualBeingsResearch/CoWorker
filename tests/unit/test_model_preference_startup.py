from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

from coworker import application
from coworker.api.admin import router_module as admin
from coworker.brain.brain import Brain
from coworker.brain.routing import ModelRouter
from coworker.core.config import Config
from coworker.core.exceptions import (
    ModelNotSupportedError,
    ProviderNotFoundError,
    RestartRequestedException,
)
from coworker.core.llm_errors import FailureCategory, ModelPreferencePending, ProviderCallError
from coworker.core.model_config import RuntimeModelConfig, load_runtime_model_config
from coworker.core.startup_intent import write_bootstrap_startup_intent
from coworker.i18n import locale_context
from coworker.memory.short_term import ShortTermMemory
from coworker.tools.system_tools import RestartSelfTool
from tests.conftest import MockProvider
from tests.unit.test_model_routing import Clock


class StartupProvider(MockProvider):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.provider_name = name
        self.connection_revision = f"startup-{name}"
        self.calls = 0
        self.failure: ProviderCallError | None = None

    async def complete(self, *args, **kwargs):
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return await super().complete(*args, **kwargs)


def test_execution_only_snapshots_cannot_create_a_preference() -> None:
    runtime = RuntimeModelConfig.from_brain_snapshot(
        {"active": {"provider": "backup", "model": "backup-model"}}
    )
    assert runtime.preferred is None


@pytest.fixture
def installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for key, value in {
        "LLM__DEFAULT_PROVIDER": "deepseek",
        "LLM__DEFAULT_MODEL": "deepseek-v4-pro",
        "LLM__PROVIDERS_FILE": "",
        "LLM__RUNTIME_CONFIG_FILE": str(tmp_path / "runtime.json"),
        "LLM__ROUTING": '{"max_attempts":7,"retry_delay_seconds":0}',
        "ADMIN__CONFIG_FILE": str(tmp_path / "admin.json"),
        "MEMORY__DB_PATH": str(tmp_path / "memory"),
        "AGENT__LOGS_DIR": str(tmp_path / "logs"),
    }.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "runtime.json").write_text(
        json.dumps({"fallbacks": ["zhipu/glm-5.3-flash", "backup/backup-model"]}), encoding="utf-8"
    )
    _snapshot(tmp_path)
    return tmp_path


def _snapshot(root: Path, provider="zhipu", model="glm-5.3-flash") -> Path:
    path = root / "memory" / "short_term_snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"primary": [], "active_provider": provider, "active_model": model}),
        encoding="utf-8",
    )
    return path


def _brain(config: Config, *, router: ModelRouter | None = None) -> Brain:
    brain = Brain(config.llm.default_provider, config.llm.default_model, router=router)
    for name in ("deepseek", "zhipu", "backup"):
        brain.register_provider(StartupProvider(name))
    return brain


async def _start(*, router: ModelRouter | None = None) -> tuple[Brain, Config]:
    _, config = application._load_config_layers()
    brain = _brain(config, router=router)
    await application._validate_model_runtime_config(brain, config)
    brain.configure_persistence(
        Path(config.llm.runtime_config_file), Path(config.memory.db_path) / "health.json"
    )
    return brain, config


async def _confirm(brain: Brain, config: Config, provider="zhipu", model="glm-5.3-flash") -> None:
    await brain.switch_model(provider, model, persistence_path=Path(config.llm.runtime_config_file))


@pytest.mark.parametrize(
    "provider,model",
    [
        ("zhipu", "glm-5.3-flash"),
        ("backup", "backup-model"),
        ("deepseek", "deepseek-v4-pro"),
        ("removed", "old-model"),
        ("", ""),
        (None, "glm-5.3-flash"),
        (123, []),
    ],
)
async def test_legacy_active_never_grants_a_preference(installation: Path, provider, model) -> None:
    _snapshot(installation, provider, model)
    brain, _ = await _start()
    assert brain.preference_pending
    assert (brain.current_provider_name, brain.current_model) == ("", "")
    stored = load_runtime_model_config(installation / "runtime.json")
    assert stored is not None and stored.preferred is None
    assert stored.preference.status == "pending" and stored.preference.source == "unresolved"
    with pytest.raises(ModelPreferencePending):
        await brain.think([], "", [])
    assert all(p.calls == 0 for p in brain.router.providers.values())


@pytest.mark.parametrize(
    "runtime_exists,snapshot_state",
    [
        (True, "valid"),
        (True, "missing"),
        (True, "corrupt"),
        (False, "valid"),
        (False, "corrupt"),
    ],
)
async def test_legacy_recovery_failure_never_returns_to_pro(
    installation: Path, runtime_exists, snapshot_state
) -> None:
    if not runtime_exists:
        (installation / "runtime.json").unlink()
    snapshot = installation / "memory" / "short_term_snapshot.json"
    if snapshot_state == "missing":
        snapshot.unlink()
    elif snapshot_state == "corrupt":
        snapshot.write_text("broken snapshot", encoding="utf-8")
    brain, _ = await _start()
    assert brain.preference_pending
    with pytest.raises(ModelPreferencePending):
        await brain.think([], "", [])
    assert brain.router.providers["deepseek"].calls == 0
    snapshot.unlink(missing_ok=True)
    restarted, _ = await _start()
    assert restarted.preference_pending


@pytest.mark.parametrize("snapshot_state", ["changed", "missing", "corrupt"])
async def test_confirmed_preference_survives_later_restarts(
    installation: Path, snapshot_state
) -> None:
    brain, config = await _start()
    await _confirm(brain, config)
    saved = (installation / "runtime.json").read_bytes()
    snapshot = _snapshot(installation, "deepseek", "deepseek-v4-pro")
    if snapshot_state == "missing":
        snapshot.unlink()
    elif snapshot_state == "corrupt":
        snapshot.write_text("broken snapshot", encoding="utf-8")
    restarted, _ = await _start()
    assert not restarted.preference_pending and restarted.current_provider_name == "zhipu"
    assert (installation / "runtime.json").read_bytes() == saved


@pytest.mark.parametrize("use_dotenv", [True, False])
async def test_saved_preference_wins_over_all_startup_sources(
    installation: Path, monkeypatch, use_dotenv
) -> None:
    if use_dotenv:
        monkeypatch.delenv("COWORKER_IGNORE_DOTENV", raising=False)
        (installation / ".env").write_text(
            "LLM__DEFAULT_PROVIDER=deepseek\nLLM__DEFAULT_MODEL=deepseek-v4-pro\n", encoding="utf-8"
        )
    (installation / "admin.json").write_text(
        json.dumps({"llm": {"default_provider": "deepseek", "default_model": "deepseek-v4-pro"}}),
        encoding="utf-8",
    )
    (installation / "runtime.json").write_text(
        json.dumps({"preferred": {"provider": "zhipu", "model": "glm-5.3-flash"}}), encoding="utf-8"
    )
    _snapshot(installation, "backup", "backup-model")
    brain, config = await _start()
    assert brain.current_provider_name == config.llm.default_provider == "zhipu"
    assert brain.model_config_snapshot()["preference"]["source"] == "legacy_config"
    await brain.think([], "", [])
    assert brain.router.providers["deepseek"].calls == 0


async def test_audit_selection_is_a_hint_not_permission_to_run(installation: Path) -> None:
    _snapshot(installation, "backup", "backup-model")
    logs = installation / "logs"
    logs.mkdir()
    event = {
        "ts": "2026-10-21T10:00:00",
        "action": "model.switch",
        "target": "zhipu/glm-5.3-flash",
        "result": "ok",
    }
    invalid = {**event, "ts": "bad", "target": "deepseek/deepseek-v4-pro"}
    (logs / "admin_audit.jsonl").write_text(
        json.dumps(event) + "\nnot-json\n" + json.dumps(invalid), encoding="utf-8"
    )
    brain, _ = await _start()
    state = brain.model_config_snapshot()["preference"]
    assert state["candidate"] == {"provider": "zhipu", "model": "glm-5.3-flash"}
    assert state["candidate_source"] == "admin_audit"
    assert brain.preference_pending and brain.model_config_snapshot()["preferred"] is None


async def test_pending_policy_edits_cannot_choose_a_model(installation: Path) -> None:
    brain, _ = await _start()
    await brain.update_model_config(
        thinking_effort="low",
        routing={"max_attempts": 9},
        summary_provider="backup",
        summary_model="summary-model",
        vision_provider="backup",
        vision_model="vision-model",
        fallbacks=["backup/backup-model"],
    )
    stored = load_runtime_model_config(installation / "runtime.json")
    assert stored.preferred is None and stored.preference.status == "pending"
    assert stored.thinking_effort == "low" and stored.routing.max_attempts == 9
    assert stored.summary.model == "summary-model" and stored.vision.model == "vision-model"
    restarted, _ = await _start()
    assert restarted.preference_pending and restarted.fallbacks == ["backup/backup-model"]


@pytest.mark.parametrize(
    "provider,model,error",
    [("removed", "missing", ProviderNotFoundError), ("zhipu", "no-tools", ModelNotSupportedError)],
)
async def test_invalid_confirmation_cannot_enable_calls(
    installation: Path, provider, model, error
) -> None:
    brain, config = await _start()
    brain.router.providers["zhipu"].declare_model_capabilities(
        "no-tools", tools=False, vision=False, video=False
    )
    before = (installation / "runtime.json").read_bytes()
    with pytest.raises(error):
        await _confirm(brain, config, provider, model)
    assert brain.preference_pending and (installation / "runtime.json").read_bytes() == before


@pytest.mark.parametrize("phase", ["pending", "confirmation"])
async def test_write_failure_does_not_enable_an_unpersisted_choice(
    installation: Path, monkeypatch, phase
) -> None:
    if phase == "confirmation":
        brain, config = await _start()
    else:
        config = application._load_config()
        brain = _brain(config)
    before = (installation / "runtime.json").read_bytes()

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("coworker.core.model_config.write_runtime_model_config", fail)
    with pytest.raises(OSError, match="disk full"):
        if phase == "confirmation":
            await _confirm(brain, config)
        else:
            await application._validate_model_runtime_config(brain, config)
    assert (installation / "runtime.json").read_bytes() == before
    if phase == "confirmation":
        assert brain.preference_pending
    assert all(p.calls == 0 for p in brain.router.providers.values())


async def test_atomic_replace_failure_keeps_pending_state(installation: Path, monkeypatch) -> None:
    brain, config = await _start()
    path, original = installation / "runtime.json", Path.replace
    before = path.read_bytes()

    def fail_replace(source, target):
        if target == path:
            raise OSError("replace failed")
        return original(source, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        await _confirm(brain, config)
    assert brain.preference_pending and path.read_bytes() == before


@pytest.mark.parametrize("locale", ["en", "zh-CN"])
async def test_pending_notice_is_localized(installation: Path, locale) -> None:
    config, messages = application._load_config(), []
    sink = logger.add(lambda message: messages.append(str(message)), format="{message}")
    try:
        with locale_context(locale):
            await application._validate_model_runtime_config(_brain(config), config)
    finally:
        logger.remove(sink)
    expected = "model calls are paused" if locale == "en" else "模型调用已暂停"
    assert any(expected in message for message in messages)


async def test_fresh_install_saves_startup_provenance(installation: Path) -> None:
    (installation / "runtime.json").unlink()
    (installation / "memory" / "short_term_snapshot.json").unlink()
    brain, _ = await _start()
    assert not brain.preference_pending and brain.current_provider_name == "deepseek"
    assert brain.model_config_snapshot()["preference"]["source"] == "startup"
    _snapshot(installation, "backup", "backup-model")
    restarted, _ = await _start()
    assert not restarted.preference_pending and restarted.current_provider_name == "deepseek"


@pytest.mark.parametrize(
    "artifact",
    ["logs/admin_audit.jsonl", "memory/instance_status.json", "memory/model_routing_state.json"],
)
async def test_missing_config_and_snapshot_do_not_hide_an_existing_installation(
    installation: Path, artifact: str
) -> None:
    (installation / "runtime.json").unlink()
    (installation / "memory" / "short_term_snapshot.json").unlink()
    marker = installation / artifact
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}", encoding="utf-8")
    brain, _ = await _start()
    assert brain.preference_pending
    assert load_runtime_model_config(installation / "runtime.json").preferred is None


async def test_bootstrap_explicit_choice_wins_over_old_state(installation: Path) -> None:
    await _start()
    write_bootstrap_startup_intent(
        installation / "memory", provider="deepseek", model="deepseek-v4-pro"
    )
    brain, _ = await _start()
    assert not brain.preference_pending and brain.current_provider_name == "deepseek"
    stored = load_runtime_model_config(installation / "runtime.json")
    assert stored.preferred.provider == "deepseek" and stored.preference.source == "explicit"


@pytest.mark.parametrize("payload", ["broken runtime", "[]", '{"preference":{"version":999}}'])
async def test_corrupt_runtime_is_not_overwritten(installation: Path, payload) -> None:
    path = installation / "runtime.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        await _start()
    assert path.read_text(encoding="utf-8") == payload


@pytest.mark.parametrize("locale", ["en", "zh-CN"])
@pytest.mark.parametrize(
    "state,preferred",
    [
        ({"status": "pending"}, {"provider": "zhipu", "model": "glm-5.3-flash"}),
        ({"status": "ready", "source": "explicit"}, None),
    ],
)
async def test_contradictory_preference_metadata_cannot_start_a_model(
    installation: Path, locale, state, preferred
) -> None:
    path = installation / "runtime.json"
    path.write_text(json.dumps({"preferred": preferred, "preference": state}), encoding="utf-8")
    before = path.read_bytes()
    with locale_context(locale), pytest.raises(ValueError):
        await _start()
    assert path.read_bytes() == before


async def test_pending_blocks_children_background_and_sync_routes(installation: Path) -> None:
    brain, config = await _start()
    child = Brain(
        "backup", "backup-model", router=brain.router, routing_parent=brain, routing_role="bubble"
    )
    await child.switch_model("backup", "backup-model")
    assert brain.preference_pending
    for call in (
        lambda: brain.think([], "", []),
        lambda: brain.summarize([]),
        lambda: child.think([], "", []),
    ):
        with pytest.raises(ModelPreferencePending):
            await call()
    target = brain.router.target("backup", "backup-model")

    def forbidden(*args):
        pytest.fail("SDK must not be called while confirmation is pending")

    with pytest.raises(ModelPreferencePending):
        brain.router.execute_sync([target], brain.routing_config, forbidden, role="long_term")
    with pytest.raises(ModelPreferencePending):
        await brain.router.execute_async([target], brain.routing_config, forbidden, role="vision")
    assert all(p.calls == 0 for p in brain.router.providers.values())
    await _confirm(brain, config)
    await child.think([], "", [])
    assert not brain.preference_pending and brain.current_provider_name == "zhipu"


async def test_admin_switch_updates_preference_every_time(installation: Path, monkeypatch) -> None:
    brain, config = await _start()
    monkeypatch.setattr(admin, "_brain", brain)
    monkeypatch.setattr(admin, "_config", config)
    app = FastAPI()
    app.include_router(admin.router)
    config.admin.token = "demo-admin-token-not-valid"
    with TestClient(app) as client:
        for index, (provider, model) in enumerate(
            [
                ("zhipu", "glm-5.3-flash"),
                ("backup", "backup-model"),
                ("zhipu", "glm-5.3-flash"),
                ("zhipu", "glm-5.3-flash"),
            ]
        ):
            response = client.post(
                "/api/admin/model/switch",
                headers={"Authorization": "Bearer demo-admin-token-not-valid"},
                json={"provider": provider, "model_id": model},
            )
            assert response.status_code == 200
            state = response.json()
            assert state["preferred"] == {"provider": provider, "model": model}
            assert (
                state["preference"]["status"] == "ready"
                and state["preference"]["source"] == "explicit"
            )
            assert state["preference"]["selected_at"] and state["restart_required"] is (index == 0)
            stored = load_runtime_model_config(installation / "runtime.json")
            assert stored.preferred.model_dump() == state["preferred"]
            restarted, _ = await _start()
            assert (restarted.current_provider_name, restarted.current_model) == (provider, model)


async def test_fallback_never_replaces_explicit_preference(installation: Path) -> None:
    brain, config = await _start()
    await _confirm(brain, config)
    brain.router.providers["zhipu"].failure = ProviderCallError(FailureCategory.AUTHENTICATION)
    assert (await brain.think([], "", [])).provider == "backup"
    await brain.summarize([])
    child = Brain("", "", router=brain.router, policy_parent=brain, routing_role="bubble")
    await child.think([], "", [])
    _snapshot(installation, "backup", "backup-model")
    restarted, _ = await _start()
    assert restarted.current_provider_name == "zhipu"
    assert restarted.model_config_snapshot()["preference"]["source"] == "explicit"
    assert brain.router.providers["deepseek"].calls == 0


async def test_legacy_alias_is_only_a_candidate_until_confirmed(installation: Path) -> None:
    _snapshot(installation, "opencode-go", "deepseek-v4-flash")
    brain, config = await _start()
    brain.register_provider(StartupProvider("opencode-go"))
    assert (
        brain.model_config_snapshot()["preference"]["candidate"]["model"] == "deepseek-v4.1-flash"
    )
    assert brain.preference_pending
    await _confirm(brain, config, "opencode-go", "deepseek-v4.1-flash")
    assert (
        load_runtime_model_config(installation / "runtime.json").preferred.model
        == "deepseek-v4.1-flash"
    )


async def test_confirmed_preference_recovers_after_restart_with_saved_quota(installation: Path) -> None:
    clock = Clock()
    brain, config = await _start(router=ModelRouter(clock=clock, wall_clock=clock))
    await _confirm(brain, config)
    saved = (installation / "runtime.json").read_bytes()
    brain.router.providers["zhipu"].failure = ProviderCallError(
        FailureCategory.QUOTA, connection_scope=True, retry_after=120
    )
    assert (await brain.think([], "", [])).provider == "backup"

    restarted, _ = await _start(router=ModelRouter(clock=clock, wall_clock=clock))
    primary = restarted.router.providers["zhipu"]
    assert restarted.current_provider_name == "zhipu"
    assert primary.calls == 0
    assert (await restarted.think([], "", [])).provider == "backup"
    assert primary.calls == 0  # The saved cooldown is honored across restart.
    clock.advance(120)
    assert primary.calls == 0  # Expiry alone does not send a paid probe.
    assert (await restarted.think([], "", [])).provider == "zhipu"
    assert restarted.router.snapshot()[0]["recovery_successes"] == 1
    assert (await restarted.think([], "", [])).provider == "zhipu"
    assert restarted.router.snapshot() == []
    assert (installation / "runtime.json").read_bytes() == saved
    assert restarted.router.providers["deepseek"].calls == 0


async def test_failed_admin_switch_keeps_inflight_call_and_saved_preference(
    installation: Path, monkeypatch
) -> None:
    brain, config = await _start()
    await _confirm(brain, config)
    saved = (installation / "runtime.json").read_bytes()
    primary = brain.router.providers["zhipu"]
    started, release = asyncio.Event(), asyncio.Event()
    original = primary.complete

    async def blocked(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    primary.complete = blocked
    running = asyncio.create_task(brain.think([], "", []))
    try:
        await asyncio.wait_for(started.wait(), 2)
        monkeypatch.setattr(admin, "_brain", brain)
        monkeypatch.setattr(admin, "_config", config)
        config.admin.token = "demo-admin-token-not-valid"
        app = FastAPI()
        app.include_router(admin.router)

        def fail(*args):
            raise OSError("disk full")

        monkeypatch.setattr("coworker.core.model_config.write_runtime_model_config", fail)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/admin/model/switch",
                headers={"Authorization": "Bearer demo-admin-token-not-valid"},
                json={"provider": "backup", "model_id": "backup-model"},
            )
        assert response.status_code == 400
        assert not running.done()
        assert brain.current_provider_name == "zhipu"
        assert (installation / "runtime.json").read_bytes() == saved
        release.set()
        assert (await asyncio.wait_for(running, 2)).provider == "zhipu"
        assert brain.router.providers["backup"].calls == 0
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_restart_precheck_ignores_stale_snapshot(installation: Path, monkeypatch) -> None:
    path = _snapshot(installation, "removed-provider", "old-model")
    config = application._load_config()
    config.memory.backend = "file"
    monkeypatch.setattr(application, "_load_config", lambda: config)
    monkeypatch.setattr(application, "_setup_logging", lambda *args: None)
    monkeypatch.setattr(
        application,
        "_register_providers",
        lambda brain, _: [
            brain.register_provider(p) for p in _brain(config).router.providers.values()
        ],
    )
    monkeypatch.setattr(
        application,
        "Identity",
        lambda *args: SimpleNamespace(load=lambda: None, detect_location=lambda: None),
    )
    before = (installation / "runtime.json").read_bytes()
    assert await application._run_check() == 0
    assert (installation / "runtime.json").read_bytes() == before

    def check(*args, **kwargs):
        return SimpleNamespace(returncode=asyncio.run(application._run_check()), stderr=b"")

    monkeypatch.setattr("coworker.tools.system_tools.subprocess.run", check)
    memory = ShortTermMemory()
    memory.active_provider, memory.active_model = "zhipu", "glm-5.3-flash"
    with pytest.raises(RestartRequestedException):
        await RestartSelfTool(memory, path).execute()
    assert json.loads(path.read_text())["active_provider"] == "zhipu"
    pending, _ = await _start()
    assert pending.preference_pending


async def test_real_startup_keeps_admin_available_and_restarts_after_confirmation(
    installation: Path, monkeypatch
) -> None:
    for key, value in {
        "MEMORY__BACKEND": "file",
        "ADMIN__TOKEN": "demo-admin-token-not-valid",
        "AGENT__TICK": "true",
        "AGENT__SUBCONSCIOUS_THINKING": "true",
    }.items():
        monkeypatch.setenv(key, value)
    providers = [StartupProvider(name) for name in ("deepseek", "zhipu", "backup")]
    monkeypatch.setattr(
        application,
        "_register_providers",
        lambda brain, _: [brain.register_provider(p) for p in providers],
    )
    monkeypatch.setattr(application.Identity, "detect_location", lambda self: None)
    monkeypatch.setattr(application.RelayClient, "start", AsyncMock())
    monkeypatch.setattr(application.TunnelManager, "start_if_configured", AsyncMock())
    monkeypatch.setattr(application.SyncService, "start", AsyncMock())

    # Exercise the real ASGI middleware and endpoints without binding a port.
    class Server:
        def __init__(self, *args):
            self.should_exit = False

        async def serve(self):
            transport = httpx.ASGITransport(app=application.api_app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {"Authorization": "Bearer demo-admin-token-not-valid"}
                page = await client.get("/admin?section=models")
                assert page.status_code == 200
                response = await client.get("/api/admin/model", headers=headers)
                assert response.status_code == 200
                assert response.json()["preference"]["status"] == "pending"
                assert not admin._agent.state.is_running
                assert all(p.calls == 0 for p in providers)
                selected = await client.post(
                    "/api/admin/model/switch",
                    headers=headers,
                    json={"provider": "zhipu", "model_id": "glm-5.3-flash"},
                )
                assert selected.status_code == 200 and selected.json()["restart_required"]
                assert admin._agent.state.restart_requested
                assert admin._agent.state.restart_reason == "preference"
            while not self.should_exit:
                await asyncio.sleep(0.001)

    monkeypatch.setattr(application.uvicorn, "Server", Server)
    assert await asyncio.wait_for(application._main(), timeout=15) is True
    saved = load_runtime_model_config(installation / "runtime.json")
    assert saved.preferred.provider == "zhipu" and saved.preference.source == "explicit"
    assert all(p.calls == 0 for p in providers)


def test_deferred_memory_factory_does_not_resolve_a_legacy_model(
    installation: Path, monkeypatch
) -> None:
    from coworker.memory import factory

    config = application._load_config()
    config.memory.backend = "mem0"
    captured = {}

    class Backend:
        backend_id = "mem0"

        @classmethod
        def available(cls):
            return True

        def __init__(self, **kwargs):
            captured.update(kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("pending startup must not resolve the legacy memory model")

    monkeypatch.setattr(factory, "_load_backends", lambda: {"mem0": Backend})
    monkeypatch.setattr(factory, "build_memory_llm_config", forbidden)
    factory.build_long_term_backend(config, defer_llm=True)
    assert captured["llm"] is None


@pytest.mark.parametrize("winerror", [5, 32, 33])
async def test_windows_file_lock_is_retried_before_applying_preference(
    installation: Path, monkeypatch, winerror: int
) -> None:
    brain, config = await _start()
    path = installation / "runtime.json"
    original = Path.replace
    attempts = []

    def replace(source, target):
        if target == path:
            attempts.append(target)
            if len(attempts) < 3:
                error = PermissionError("temporary file lock")
                error.winerror = winerror
                raise error
        return original(source, target)

    monkeypatch.setattr(Path, "replace", replace)
    await _confirm(brain, config)
    assert len(attempts) == 3
    assert not brain.preference_pending
    assert load_runtime_model_config(path).preferred.provider == "zhipu"


@pytest.mark.parametrize("winerror,attempt_limit", [(5, 4), (None, 1)])
async def test_persistent_file_denial_never_enables_an_unsaved_preference(
    installation: Path, monkeypatch, winerror, attempt_limit: int
) -> None:
    brain, config = await _start()
    path = installation / "runtime.json"
    before = path.read_bytes()
    attempts = []

    def replace(source, target):
        attempts.append(target)
        error = PermissionError("persistent denial")
        if winerror is not None:
            error.winerror = winerror
        raise error

    monkeypatch.setattr(Path, "replace", replace)
    with pytest.raises(PermissionError):
        await _confirm(brain, config)
    assert len(attempts) == attempt_limit
    assert brain.preference_pending and path.read_bytes() == before
