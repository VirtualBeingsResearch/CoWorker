from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai
import pytest

from coworker.brain.brain import Brain
from coworker.brain.factory import build_provider
from coworker.brain.routing import ModelRouter
from coworker.core.exceptions import ProviderError
from coworker.core.llm_errors import (
    FailureCategory,
    ModelRoutingUnavailable,
    ProviderCallError,
    normalize_provider_error,
)
from coworker.core.model_config import RuntimeModelConfig, load_runtime_model_config
from coworker.core.model_routing import ModelRoutingConfig
from coworker.core.types import LLMResponse
from tests.conftest import MockProvider


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ScenarioProvider(MockProvider):
    def __init__(self, name: str, failure: ProviderCallError | None = None) -> None:
        super().__init__()
        self.provider_name = name
        self.provider_type = "zhipu"
        self.default_model = f"{name}-model"
        self.connection_revision = f"revision-{name}"
        self.failure = failure
        self.calls: list[str] = []

    async def complete(self, messages, system_prompt, tools, max_tokens=4096, **kwargs):
        self.calls.append(self._current_model)
        if self.failure is not None:
            raise self.failure
        return LLMResponse(self.provider_name, [], "end_turn", self._current_model, {})


def make_brain(*, category=FailureCategory.TRANSIENT, **policy):
    clock = Clock()
    router = ModelRouter(clock=clock, wall_clock=clock)
    config = ModelRoutingConfig(retry_delay_seconds=0, **policy)
    primary = ScenarioProvider("primary", ProviderCallError(category))
    backup = ScenarioProvider("backup")
    brain = Brain("primary", "primary-model", fallbacks=["backup"], router=router, routing=config)
    brain.register_provider(primary)
    brain.register_provider(backup)
    return brain, primary, backup, clock


async def think(brain: Brain):
    return await brain.think([], "", [])


@pytest.mark.parametrize(
    ("status", "code", "category", "connection_scope"),
    [
        (400, "1301", FailureCategory.CONTENT_POLICY, False),
        (400, "1261", FailureCategory.CONTEXT_LENGTH, False),
        (429, "1302", FailureCategory.RATE_LIMIT, True),
        (429, "1305", FailureCategory.TRANSIENT, False),
        (429, "1308", FailureCategory.QUOTA, True),
        (429, "1310", FailureCategory.QUOTA, True),
        (429, "1316", FailureCategory.QUOTA, True),
        (429, "1309", FailureCategory.PERMISSION, True),
        (429, "1113", FailureCategory.QUOTA, True),
        (429, "1311", FailureCategory.PERMISSION, False),
        (403, "1220", FailureCategory.PERMISSION, False),
        (401, "api_key_expired", FailureCategory.AUTHENTICATION, True),
        (429, "insufficient_quota", FailureCategory.QUOTA, True),
        (402, "", FailureCategory.QUOTA, True),
        (429, "", FailureCategory.RATE_LIMIT, False),
        (503, "", FailureCategory.TRANSIENT, False),
        (422, "", FailureCategory.INVALID_REQUEST, False),
    ],
)
def test_structured_sdk_error_classification(status, code, category, connection_scope):
    response = httpx.Response(status, request=httpx.Request("POST", "https://example.invalid/v1"))
    raw = openai.APIStatusError(
        "sensitive-body-must-not-be-logged", response=response, body={"error": {"code": code}}
    )
    wrapped = ProviderError("legacy SDK wrapper")
    wrapped.__cause__ = raw
    failure = normalize_provider_error(wrapped, "zhipu")
    assert failure is not None
    assert failure.category == category
    assert failure.connection_scope is connection_scope
    assert "sensitive-body" not in str(failure)
    assert "sensitive-body" not in json.dumps(failure.as_dict())


def test_retry_after_and_explicit_quota_reset_are_not_shortened():
    response = httpx.Response(
        429,
        headers={"Retry-After": "120"},
        request=httpx.Request("POST", "https://example.invalid"),
    )
    error = openai.RateLimitError(
        "quota", response=response, body={"error": {"code": "1308", "reset_at": 5000}}
    )
    failure = normalize_provider_error(error, "zhipu", now=1000)
    assert failure is not None and failure.retry_after == 4000


def test_unknown_programming_error_is_not_a_provider_outage():
    assert normalize_provider_error(RuntimeError("implementation bug")) is None


def test_sdk_envelope_wins_over_deeper_transport_cause():
    response = httpx.Response(400, request=httpx.Request("POST", "https://example.invalid"))
    error = openai.BadRequestError(
        "private body", response=response, body={"error": {"code": "1301"}}
    )
    error.__cause__ = httpx.ConnectError("lower layer")
    failure = normalize_provider_error(error, "zhipu")
    assert failure is not None and failure.category == FailureCategory.CONTENT_POLICY


def test_go_forwarded_glm_policy_code_is_not_treated_as_a_rate_limit():
    response = httpx.Response(429, request=httpx.Request("POST", "https://example.invalid"))
    error = openai.RateLimitError(
        "private body", response=response, body={"error": {"code": "1301"}}
    )
    failure = normalize_provider_error(error, "opencode-go")
    assert failure is not None and failure.category == FailureCategory.CONTENT_POLICY


def test_nested_sdk_transport_fault_keeps_retry_classification_and_connection_scope():
    transport = httpx.ConnectError("private endpoint")
    transport.__cause__ = OSError("socket detail")
    sdk = openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
    sdk.__cause__ = transport
    outer = ProviderError("legacy wrapper")
    outer.__cause__ = sdk
    failure = normalize_provider_error(outer)
    assert failure is not None and failure.category == FailureCategory.TRANSIENT
    assert failure.connection_scope is True
    assert "socket detail" not in str(failure)


@pytest.mark.asyncio
async def test_temporary_fallback_preserves_preference_and_next_call_uses_primary():
    brain, primary, backup, _ = make_brain()
    response = await think(brain)
    assert response.provider == "backup"
    assert primary.calls == ["primary-model", "primary-model"]
    assert backup.calls == ["backup-model"]
    assert brain.current_provider_name == "primary"
    primary.failure = None
    assert (await think(brain)).provider == "primary"
    assert brain.model_config_snapshot()["last_used"]["fallback"] is False


@pytest.mark.asyncio
async def test_breaker_counts_logical_failures_not_internal_attempts():
    brain, primary, _, clock = make_brain()
    for _ in range(3):
        assert (await think(brain)).provider == "backup"
    assert len(primary.calls) == 6
    assert (await think(brain)).provider == "backup"
    assert len(primary.calls) == 6
    clock.advance(60)
    primary.failure = None
    assert (await think(brain)).provider == "primary"
    assert brain.router.snapshot()[0]["state"] == "half_open"
    assert (await think(brain)).provider == "primary"
    assert brain.router.snapshot() == []
    # Closing the breaker must also release its probe lease.
    assert (await think(brain)).provider == "primary"


@pytest.mark.asyncio
async def test_failed_recovery_extends_cooldown():
    brain, primary, _, clock = make_brain(failure_threshold=1)
    await think(brain)
    clock.advance(60)
    await think(brain)
    state = brain.router.snapshot()[0]
    assert state["state"] == "open"
    assert state["cooldown_seconds"] == 120
    count = len(primary.calls)
    clock.advance(119)
    await think(brain)
    assert len(primary.calls) == count


@pytest.mark.asyncio
async def test_authentication_failure_is_once_per_connection_until_replaced():
    brain, primary, _, clock = make_brain()
    primary.failure = ProviderCallError(FailureCategory.AUTHENTICATION, connection_scope=True)
    await think(brain)
    clock.advance(100000)
    await think(brain)
    assert len(primary.calls) == 1
    # A different model on the same connection must not bypass bad credentials.
    await brain.update_model_config(fallbacks=["primary/other-model", "backup"])
    await think(brain)
    assert len(primary.calls) == 1
    replacement = ScenarioProvider("primary")
    replacement.connection_revision = "new-credentials"
    await brain.upsert_provider(replacement)
    assert (await think(brain)).provider == "primary"


@pytest.mark.asyncio
async def test_model_specific_failure_does_not_disable_another_model():
    brain, primary, _, _ = make_brain()
    primary.failure = ProviderCallError(FailureCategory.MODEL_UNAVAILABLE)
    await think(brain)
    primary.failure = None
    await brain.update_model_config(fallbacks=["primary/other-model", "backup"])
    response = await think(brain)
    assert response.provider == "primary" and response.model == "other-model"


@pytest.mark.asyncio
async def test_quota_waits_for_server_reset_and_then_validates():
    brain, primary, _, clock = make_brain()
    primary.failure = ProviderCallError(
        FailureCategory.QUOTA, connection_scope=True, retry_after=3600
    )
    await think(brain)
    clock.advance(3599)
    await think(brain)
    assert len(primary.calls) == 1
    clock.advance(1)
    primary.failure = None
    assert (await think(brain)).provider == "primary"


@pytest.mark.parametrize(
    "category",
    [
        FailureCategory.CONTENT_POLICY,
        FailureCategory.CONTEXT_LENGTH,
        FailureCategory.INVALID_REQUEST,
    ],
)
@pytest.mark.asyncio
async def test_request_error_is_not_retried_fallen_back_or_counted_as_outage(category):
    brain, primary, backup, _ = make_brain(category=category)
    with pytest.raises(ProviderCallError) as raised:
        await think(brain)
    assert raised.value.category == category
    assert len(primary.calls) == 1
    assert backup.calls == []
    assert brain.router.snapshot() == []


@pytest.mark.asyncio
async def test_only_one_recovery_probe_across_brains_and_cancellation_releases_it():
    brain, primary, _, clock = make_brain(failure_threshold=1)
    await think(brain)
    clock.advance(60)
    primary.failure = None
    entered, release = asyncio.Event(), asyncio.Event()
    original = primary.complete

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    primary.complete = blocked
    child = Brain(
        "primary", "primary-model", router=brain.router, policy_parent=brain, routing_role="bubble"
    )
    probing = asyncio.create_task(think(brain))
    await entered.wait()
    assert (await think(child)).provider == "backup"
    probing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probing
    release.set()
    assert (await think(child)).provider == "primary"


@pytest.mark.asyncio
async def test_manual_preference_change_cannot_be_overwritten_by_old_response():
    brain, primary, _, _ = make_brain()
    primary.failure = None
    entered, release = asyncio.Event(), asyncio.Event()
    original = primary.complete

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    primary.complete = blocked
    old = asyncio.create_task(think(brain))
    await entered.wait()
    await brain.switch_model("backup", "backup-model")
    release.set()
    response = await old
    assert response.provider == "backup"
    assert brain.current_provider_name == "backup"
    assert brain.model_config_snapshot()["last_used"]["provider"] == "backup"
    assert brain.router.snapshot() == []


@pytest.mark.asyncio
async def test_one_connection_can_concurrently_execute_different_models():
    brain, primary, _, _ = make_brain()
    primary.failure = None
    arrived = 0
    both = asyncio.Event()

    async def capture(*args, **kwargs):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both.set()
        await both.wait()
        return LLMResponse("ok", [], "end_turn", primary._current_model, {})

    primary.complete = capture
    results = await asyncio.gather(
        primary.complete_for_model("first-model", [], "", []),
        primary.complete_for_model("second-model", [], "", []),
    )
    assert [response.model for response in results] == ["first-model", "second-model"]
    assert primary._current_model == "mock-model"


@pytest.mark.asyncio
async def test_preference_and_quota_health_survive_restart(tmp_path):
    brain, primary, _, clock = make_brain()
    runtime_path, health_path = tmp_path / "runtime.json", tmp_path / "health.json"
    brain.configure_persistence(runtime_path, health_path)
    await brain.switch_model("primary", "primary-model")
    primary.failure = ProviderCallError(
        FailureCategory.QUOTA, connection_scope=True, retry_after=3600
    )
    await think(brain)
    stored = load_runtime_model_config(runtime_path)
    assert stored is not None and stored.preferred is not None
    assert stored.preferred.provider == "primary"
    restarted = ModelRouter(clock=clock, wall_clock=clock)
    restarted.register(primary)
    restarted.configure_persistence(health_path)
    assert restarted.snapshot()[0]["state"] == "open"
    assert restarted.snapshot()[0]["retry_at"] == clock.now + 3600


@pytest.mark.asyncio
async def test_failed_persistence_does_not_apply_new_preference_or_policy(tmp_path, monkeypatch):
    brain, _, _, _ = make_brain()
    brain.configure_persistence(tmp_path / "runtime.json", tmp_path / "health.json")

    # The persistence function is synchronous and fails before runtime mutation.
    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(brain, "_persist_config", fail)
    with pytest.raises(OSError):
        await brain.switch_model("backup", "backup-model")
    with pytest.raises(OSError):
        await brain.update_model_config(routing={"cooldown_seconds": 120})
    assert brain.current_provider_name == "primary"
    assert brain.routing_config.cooldown_seconds == 60


def test_legacy_runtime_config_keeps_configured_default_preference():
    from coworker.core.config import LLMConfig

    runtime = RuntimeModelConfig.model_validate({"fallbacks": ["backup"]})
    assert runtime.preferred is None
    llm = LLMConfig(
        _env_file=None,
        default_provider="primary",
        default_model="primary-model",
        routing=ModelRoutingConfig(max_attempts=7),
    )
    runtime.apply_to_llm_config(llm)
    assert llm.default_provider == "primary"
    assert llm.routing.max_attempts == 7


def test_sync_and_async_routes_share_the_same_connection_breaker():
    brain, primary, backup, _ = make_brain()
    targets = brain.routing_targets()
    attempts = []

    def call(target, timeout):
        attempts.append(target.name)
        if target.name == "primary":
            raise ProviderCallError(FailureCategory.AUTHENTICATION, connection_scope=True)
        return "ok"

    first = brain.router.execute_sync(targets, brain.routing_config, call, role="long_term")
    second = brain.router.execute_sync(targets, brain.routing_config, call, role="summary")
    assert first.target.name == second.target.name == "backup"
    assert attempts == ["primary", "backup", "backup"]


@pytest.mark.asyncio
async def test_entire_request_has_one_attempt_budget():
    brain, primary, backup, _ = make_brain(max_attempts=3)
    backup.failure = ProviderCallError(FailureCategory.TRANSIENT)
    with pytest.raises(ModelRoutingUnavailable) as raised:
        await think(brain)
    assert len(primary.calls) == 2
    assert len(backup.calls) == 1
    assert len(raised.value.failures) == 3
    assert raised.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_server_retry_delay_exceeding_budget_is_not_ignored():
    brain, primary, _, _ = make_brain()
    primary.failure = ProviderCallError(FailureCategory.TRANSIENT, retry_after=600)
    assert (await think(brain)).provider == "backup"
    assert len(primary.calls) == 1
    assert brain.router.snapshot()[0]["cooldown_seconds"] == 600
    assert (await think(brain)).provider == "backup"
    assert len(primary.calls) == 1


def test_corrupt_persisted_health_is_ignored(tmp_path):
    brain, _, _, _ = make_brain()
    path = tmp_path / "health.json"
    path.write_text(
        json.dumps(
            [
                {
                    "provider": "primary",
                    "model": "primary-model",
                    "connection_revision": "revision-primary",
                    "state": "open",
                    "retry_at": float("inf"),
                }
            ]
        ),
        encoding="utf-8",
    )
    brain.router.configure_persistence(path)
    assert brain.router.snapshot() == []


@pytest.mark.asyncio
async def test_real_bubble_and_subconscious_brains_share_policy_and_health():
    from coworker.agent.subconscious import SubconsciousScheduler
    from coworker.tools.bubble_tools import _create_bubble_brain

    brain, primary, _, _ = make_brain(failure_threshold=1)
    bubble = _create_bubble_brain(brain, "primary", "primary-model", inherit_policy=True)
    scheduler = SubconsciousScheduler.__new__(SubconsciousScheduler)
    scheduler._brain = brain
    subconscious = scheduler._create_brain()
    assert bubble.router is subconscious.router is brain.router
    assert (await think(bubble)).provider == "backup"
    assert (await think(subconscious)).provider == "backup"
    assert len(primary.calls) == 2
    await brain.switch_model("backup", "backup-model")
    assert bubble.current_provider_name == subconscious.current_provider_name == "backup"
    # A child-local target override does not change the parent's preference.
    await bubble.switch_model("primary", "primary-model")
    assert brain.current_provider_name == "backup"


@pytest.mark.asyncio
async def test_explicit_bubble_target_does_not_expand_to_main_fallbacks():
    from coworker.tools.bubble_tools import _create_bubble_brain

    brain, _, backup, _ = make_brain()
    bubble = _create_bubble_brain(brain, "primary", "primary-model")
    with pytest.raises(ModelRoutingUnavailable):
        await think(bubble)
    assert backup.calls == []


@pytest.mark.asyncio
async def test_summary_and_vision_share_health_without_rewriting_main():
    brain, primary, _, clock = make_brain()
    primary.failure = ProviderCallError(
        FailureCategory.RATE_LIMIT, retry_after=30, connection_scope=True
    )
    assert await brain.summarize([]) == "backup"
    assert (await think(brain)).provider == "backup"
    assert len(primary.calls) == 1
    await brain.update_model_config(vision_provider="primary", vision_model="primary-model")
    with pytest.raises(ModelRoutingUnavailable):
        await brain.query_with_vision([])
    clock.advance(30)
    primary.failure = None
    assert await brain.query_with_vision([]) == "primary"
    assert brain.current_provider_name == "primary"


@pytest.mark.asyncio
async def test_routing_patch_preserves_other_configured_limits():
    brain, _, _, _ = make_brain(max_attempts=7)
    await brain.update_model_config(routing={"cooldown_seconds": 120})
    assert brain.routing_config.max_attempts == 7
    assert brain.routing_config.cooldown_seconds == 120


@pytest.mark.asyncio
async def test_attempt_budget_reserves_a_chance_for_the_third_configured_plan():
    brain, primary, backup, _ = make_brain()
    backup.failure = ProviderCallError(FailureCategory.TRANSIENT)
    last = ScenarioProvider("last")
    brain.register_provider(last)
    await brain.update_model_config(fallbacks=["backup", "last"])
    assert (await think(brain)).provider == "last"
    assert len(primary.calls) == 2 and len(backup.calls) == 1 and len(last.calls) == 1


@pytest.mark.asyncio
async def test_selecting_a_healthy_model_does_not_start_unnecessary_recovery():
    brain, primary, _, _ = make_brain()
    primary.failure = None
    await think(brain)
    await brain.switch_model("primary", "primary-model")
    assert brain.router.snapshot() == []


@pytest.mark.parametrize(
    "finish,refusal,empty,category",
    [
        ("sensitive", None, False, FailureCategory.CONTENT_POLICY),
        ("content_filter", None, False, FailureCategory.CONTENT_POLICY),
        ("stop", "refused", False, FailureCategory.CONTENT_POLICY),
        ("network_error", None, False, FailureCategory.TRANSIENT),
        ("stop", None, True, FailureCategory.INVALID_RESPONSE),
    ],
)
@pytest.mark.asyncio
async def test_chat_wire_http_success_is_classified_before_fallback(
    finish, refusal, empty, category
):
    brain, _, backup, _ = make_brain(failure_threshold=1)
    primary = build_provider(
        "zhipu", "demo-key-not-valid", "https://example.invalid/v1", "primary", "primary-model"
    )
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[]
            if empty
            else [SimpleNamespace(finish_reason=finish, message=SimpleNamespace(refusal=refusal))]
        )
    )
    primary._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    await brain.upsert_provider(primary)
    if category == FailureCategory.CONTENT_POLICY:
        with pytest.raises(ProviderCallError) as raised:
            await think(brain)
        assert raised.value.category == category
        assert create.await_count == 1 and backup.calls == []
        assert brain.router.snapshot() == []
    else:
        assert (await think(brain)).provider == "backup"
        assert create.await_count == 2
        assert brain.router.snapshot()[0]["error"]["category"] == category.value


@pytest.mark.asyncio
async def test_failed_responses_api_envelope_preserves_policy_classification():
    brain, _, backup, _ = make_brain()
    primary = build_provider(
        "openai",
        "demo-key-not-valid",
        "https://example.invalid/v1",
        "primary",
        "primary-model",
        tool_use_models=["primary-model"],
    )
    create = AsyncMock(
        return_value=SimpleNamespace(
            status="failed",
            error=SimpleNamespace(model_dump=lambda: {"code": "content_policy_violation"}),
        )
    )
    primary._client = SimpleNamespace(responses=SimpleNamespace(create=create))
    await brain.upsert_provider(primary)
    with pytest.raises(ProviderCallError) as raised:
        await think(brain)
    assert raised.value.category == FailureCategory.CONTENT_POLICY
    assert create.await_count == 1 and backup.calls == []


@pytest.mark.asyncio
async def test_old_connection_failure_cannot_poison_replacement():
    brain, primary, _, _ = make_brain(failure_threshold=1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_failure(*args, **kwargs):
        entered.set()
        await release.wait()
        raise ProviderCallError(FailureCategory.AUTHENTICATION, connection_scope=True)

    primary.complete = delayed_failure
    pending = asyncio.create_task(think(brain))
    await entered.wait()
    replacement = ScenarioProvider("primary")
    replacement.connection_revision = "replaced-while-in-flight"
    await brain.upsert_provider(replacement)
    release.set()
    assert (await pending).provider == "backup"
    assert brain.router.snapshot() == []
    assert (await think(brain)).provider == "primary"


@pytest.mark.asyncio
async def test_attempt_timeout_leaves_time_for_fallback():
    brain, primary, _, _ = make_brain(attempt_timeout_seconds=0.01, request_timeout_seconds=0.1)
    never = asyncio.Event()

    async def stall(*args, **kwargs):
        await never.wait()

    primary.complete = stall
    assert (await asyncio.wait_for(think(brain), timeout=1)).provider == "backup"
    assert brain.current_provider_name == "primary"


@pytest.mark.asyncio
async def test_server_wait_immediately_cools_target_for_concurrent_requests():
    brain, primary, backup, clock = make_brain()
    primary.failure = ProviderCallError(FailureCategory.TRANSIENT, retry_after=60)
    # The server delay fits the request budget, but is not a private sleep: all
    # callers must see the cooldown before the first request uses its fallback.
    response = await asyncio.wait_for(think(brain), timeout=1)
    assert response.provider == "backup"
    assert brain.router.snapshot()[0]["retry_at"] == clock.now + 60
    assert all(r.provider == "backup" for r in await asyncio.gather(think(brain), think(brain)))
    assert primary.calls == ["primary-model"]
    assert len(backup.calls) == 3
    clock.advance(60)
    primary.failure = None
    assert (await think(brain)).provider == "primary"
    assert brain.router.snapshot()[0]["recovery_successes"] == 1
    assert (await think(brain)).provider == "primary"
    assert brain.router.snapshot() == []


def test_sync_server_wait_cools_without_sleeping(monkeypatch):
    brain, _, _, _ = make_brain()

    def no_sleep(_seconds):
        pytest.fail("server-directed waiting must be shared, not a private retry sleep")

    monkeypatch.setattr("coworker.brain.routing.time.sleep", no_sleep)
    calls = []

    def call(target, _timeout):
        calls.append(target.name)
        if target.name == "primary":
            raise ProviderCallError(FailureCategory.TRANSIENT, retry_after=60)
        return "ok"

    for _ in range(2):
        assert brain.router.execute_sync(
            brain.routing_targets(), brain.routing_config, call, role="long_term"
        ).target.name == "backup"
    assert calls == ["primary", "backup", "backup"]


def test_server_wait_is_shared_before_observability_callbacks_run():
    brain, _, _, _ = make_brain()
    observed = []

    def on_route(event):
        if event["outcome"] == "failed" and not observed:
            result = brain.router.execute_sync(
                brain.routing_targets(), brain.routing_config,
                lambda target, _timeout: target.name, role="long_term",
            )
            observed.append(result.target.name)

    brain.router.add_listener(on_route)

    def call(target, _timeout):
        if target.name == "primary":
            raise ProviderCallError(FailureCategory.TRANSIENT, retry_after=60)
        return "ok"

    brain.router.execute_sync(brain.routing_targets(), brain.routing_config, call, role="main")
    assert observed == ["backup"]


@pytest.mark.parametrize("inherit_preference", [False, True])
@pytest.mark.asyncio
async def test_bubbles_keep_live_routing_limits_after_target_override(inherit_preference):
    from coworker.tools.bubble_tools import _create_bubble_brain

    brain, primary, _, _ = make_brain()
    bubble = _create_bubble_brain(
        brain, "primary", "primary-model", inherit_policy=inherit_preference
    )
    if inherit_preference:
        await bubble.switch_model("primary", "primary-model")
    await brain.update_model_config(routing={"quota_cooldown_seconds": 30})
    await brain.switch_model("backup", "backup-model")
    assert bubble.current_provider_name == "primary"
    assert bubble.routing_config.quota_cooldown_seconds == 30
    primary.failure = ProviderCallError(FailureCategory.QUOTA, connection_scope=True)
    if inherit_preference:
        assert (await think(bubble)).provider == "backup"
    else:
        with pytest.raises(ModelRoutingUnavailable):
            await think(bubble)
    assert brain.router.snapshot()[0]["cooldown_seconds"] == 30


@pytest.mark.asyncio
async def test_retry_deadline_combines_connection_and_model_constraints():
    brain, primary, _, clock = make_brain(failure_threshold=1)
    await think(brain)  # Model breaker: 60 seconds.
    primary.failure = ProviderCallError(FailureCategory.QUOTA, connection_scope=True)
    await brain.update_model_config(fallbacks=["primary/other-model", "backup"])
    await think(brain)  # Same connection, different model: 1800 seconds.
    clock.advance(61)
    target = brain.router.target("primary", "primary-model")
    assert target is not None
    assert brain.router._retry_after([target]) == 1739


@pytest.mark.parametrize("wire", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("known", [True, False])
@pytest.mark.asyncio
async def test_rejected_wire_responses_keep_measured_usage_out_of_health_state(wire, known):
    brain, _, backup, _ = make_brain()
    kind = {"chat": "zhipu", "responses": "openai", "anthropic": "anthropic"}[wire]
    primary = build_provider(
        kind, "demo-key-not-valid", "https://example.invalid/v1", "primary", "primary-model",
        tool_use_models=["primary-model"],
    )
    usage = SimpleNamespace(
        input_tokens=17, output_tokens=3, cache_read_input_tokens=9,
        prompt_tokens=17, completion_tokens=3,
        prompt_tokens_details=SimpleNamespace(cached_tokens=9),
        input_tokens_details=SimpleNamespace(cached_tokens=9),
    )
    response = SimpleNamespace(model="actual-model", usage=usage if known else SimpleNamespace())
    if wire == "chat":
        response.choices = [SimpleNamespace(finish_reason="sensitive")]
        primary._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response))))
    elif wire == "responses":
        response.status = "failed"
        response.error = SimpleNamespace(model_dump=lambda: {"code": "content_policy_violation"})
        primary._client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response)))
    else:
        response.stop_reason = "refusal"
        primary._client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))
    await brain.upsert_provider(primary)
    routes = []
    brain.router.add_listener(routes.append)
    with pytest.raises(ProviderCallError) as raised:
        await think(brain)
    assert raised.value.category == FailureCategory.CONTENT_POLICY
    if known:
        assert routes[0]["usage"] == {"input_tokens": 17, "output_tokens": 3, "cached_tokens": 9}
        assert routes[0]["usage_model"] == "actual-model"
        assert routes[0]["usage_source"] == "provider"
    else:
        assert "usage" not in routes[0]
        assert routes[0]["usage_source"] == "unavailable"
    assert "usage" not in raised.value.as_dict()
    assert backup.calls == []
    assert brain.router.snapshot() == []


@pytest.mark.parametrize("role", ["main", "bubble", "subconscious"])
@pytest.mark.parametrize("operation", ["think", "summary", "vision"])
@pytest.mark.asyncio
async def test_failed_attempt_usage_retains_background_scope(role, operation):
    brain, primary, _, _ = make_brain()
    primary.failure = ProviderCallError(
        FailureCategory.CONTENT_POLICY, usage={"input_tokens": 17, "output_tokens": 3}
    )
    child = Brain(
        "primary", "primary-model", router=brain.router, policy_parent=brain, routing_role=role,
        vision_provider="primary", vision_model="primary-model",
    )
    routes = []
    brain.router.add_listener(routes.append)
    with pytest.raises(ProviderCallError):
        if operation == "think":
            await think(child)
        elif operation == "summary":
            await child._summary_think([], "")
        else:
            primary.can_use_vision = lambda _model: True
            await child.query_with_vision([])
    assert routes[0]["role"] == (role if operation == "think" else operation)
    assert routes[0]["usage_scope"] == (role if role != "main" else routes[0]["role"])


@pytest.mark.asyncio
async def test_each_failed_retry_is_accounted_before_successful_fallback():
    from coworker.agent.usage_stats import UsageStatsCollector

    brain, primary, _, _ = make_brain()
    primary.failure = ProviderCallError(
        FailureCategory.TRANSIENT, usage={"input_tokens": 17, "output_tokens": 3, "cached_tokens": 9}
    )
    collector = UsageStatsCollector()
    routes = []
    brain.router.add_listener(routes.append)
    brain.router.add_listener(lambda event: collector.on_entry({"type": "model_route", **event}))
    response = await think(brain)
    collector.on_entry({
        "type": "llm_response", "provider": response.provider, "model": response.model,
        "usage": {"input_tokens": 5, "output_tokens": 2},
    })
    stats = collector.snapshot()["lifetime"]
    assert stats["llm_calls"] == stats["exact_calls"] == 3
    assert stats["total_tokens"] == 47
    assert stats["cached_tokens"] == 18
    assert len({event["routing_id"] for event in routes}) == 1
    assert [event["attempt"] for event in routes[:2]] == [1, 2]


@pytest.mark.parametrize(
    "requested,types,default,expected",
    [
        ("glm-b", {"glm-a": "zhipu", "glm-b": "zhipu"}, "glm-a", "glm-b"),
        ("zhipu", {"glm-a": "zhipu", "glm-b": "zhipu"}, "glm-a", "glm-a"),
        ("zhipu", {"glm-a": "zhipu", "go": "opencode-go"}, "go", "glm-a"),
        ("zhipu", {"glm-a": "zhipu", "glm-b": "zhipu"}, "go", None),
        ("missing", {"glm-a": "zhipu"}, "glm-a", None),
    ],
)
def test_memory_provider_alias_resolution_contract(requested, types, default, expected):
    from coworker.memory.long_term import resolve_memory_provider_name

    assert resolve_memory_provider_name(requested, types, default) == expected


@pytest.mark.asyncio
async def test_internal_retry_rechecks_breaker_opened_by_another_request(monkeypatch):
    brain, primary, _, _ = make_brain()
    await brain.update_model_config(routing={"retry_delay_seconds": 1})
    sleeping, resume = asyncio.Event(), asyncio.Event()

    async def delay(_seconds):
        sleeping.set()
        await resume.wait()

    monkeypatch.setattr("coworker.brain.routing.asyncio.sleep", delay)
    first = asyncio.create_task(think(brain))
    try:
        await asyncio.wait_for(sleeping.wait(), timeout=1)
        primary.failure = ProviderCallError(FailureCategory.QUOTA, connection_scope=True)
        assert (await think(brain)).provider == "backup"
        resume.set()
        assert (await first).provider == "backup"
        assert len(primary.calls) == 2  # The sleeping request must not call again.
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


def test_retry_deadline_ignores_blocked_targets_and_bounds_probe_polling():
    brain, primary, _, clock = make_brain(failure_threshold=1)
    router, config = brain.router, brain.routing_config

    def fail(target, error):
        permit = router._acquire(target)
        assert permit is not None
        router._failure(permit, error, config)
        router._release(permit)

    first = router.target("primary", "primary-model")
    blocked = router.target("primary", "blocked-model")
    backup = router.target("backup", "backup-model")
    assert first is not None and blocked is not None and backup is not None
    fail(first, ProviderCallError(FailureCategory.TRANSIENT))
    fail(blocked, ProviderCallError(FailureCategory.PERMISSION))
    fail(backup, ProviderCallError(FailureCategory.RATE_LIMIT, retry_after=120))
    assert router._retry_after([blocked, first, backup]) == 60
    clock.advance(60)
    probe = router._acquire(first)
    assert probe is not None
    try:
        assert router._retry_after([blocked, first, backup]) == 30
        assert router._retry_after([blocked]) == 30
    finally:
        router._release(probe)
    replacement = ScenarioProvider("primary")
    replacement.connection_revision = "new-connection"
    router.register(replacement)
    assert router._retry_after([first, backup]) == 60  # Old revision is ineligible.


def test_failure_accounting_never_copies_non_numeric_or_sensitive_usage_fields():
    failure = ProviderCallError(
        FailureCategory.TRANSIENT,
        usage={"input_tokens": 17, "output_tokens": 3, "cached_tokens": True, "raw_body": "private"},
    )
    assert failure.usage == {"input_tokens": 17, "output_tokens": 3}
    assert "private" not in json.dumps(failure.as_dict())
    assert ProviderCallError(FailureCategory.TRANSIENT, usage={"output_tokens": "unknown"}).usage is None
    assert ProviderCallError(FailureCategory.TRANSIENT, usage={"input_tokens": 17, "output_tokens": -3}).usage is None
