"""Tests for the /metrics endpoint: auth, setup-mode gating and body content."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from coworker.agent.usage_stats import UsageStatsCollector
from coworker.api import app as api_app
from coworker.api.metrics import setup as setup_metrics
from coworker.channels.system import create_channel_system
from coworker.core.metrics import MetricsRegistry


@pytest.fixture
def client():
    import coworker.api.metrics as metrics_mod
    import coworker.api.routes as routes_mod

    metrics_mod._usage_stats = None
    metrics_mod._registry = None
    metrics_mod._agent = None
    metrics_mod._channel_traffic = None
    routes_mod._communication_token = ""
    routes_mod._communication_token_explicit = False
    api_app.set_setup_required(False)
    api_app._metrics_registry = None
    api_app._http_requests_total = None
    api_app._http_request_duration = None
    with TestClient(api_app.app) as test_client:
        yield test_client
    api_app.set_setup_required(False)


def _full_setup(tmp_path, registry: MetricsRegistry | None) -> None:
    channel_system = create_channel_system(tmp_path / "outbox", metrics=registry)
    channel_system.traffic.record(
        direction="inbound",
        channel="wecom",
        participant_id="wecom:single:allowed",
        status="received",
        source="wecom",
    )
    usage_stats = UsageStatsCollector(now_fn=lambda: datetime(2026, 6, 29, 12, 0, 0))
    usage_stats.load_entries([
        {
            "type": "message_in",
            "seq": 1,
            "ts": "2026-06-29T08:00:00",
            "participant_id": "wecom:single:allowed",
            "source": "wecom",
            "content": "hello",
        },
    ])
    agent = type(
        "AgentStub", (), {"state": type("StateStub", (), {"cycle_count": 7})()}
    )()
    setup_metrics(
        usage_stats=usage_stats,
        registry=registry,
        agent=agent,
        channel_traffic=channel_system.traffic,
    )


def test_metrics_returns_404_when_disabled(client, tmp_path):
    _full_setup(tmp_path, registry=None)

    response = client.get("/metrics")

    assert response.status_code == 404


def test_metrics_renders_text_exposition_without_token(client, tmp_path):
    registry = MetricsRegistry()
    api_app.setup_metrics_registry(registry)
    _full_setup(tmp_path, registry=registry)

    first = client.get("/metrics")
    # HTTP 计数在响应完成后落账：第二请求才能看到第一请求的计数。
    second = client.get("/metrics")

    for response in (first, second):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
        assert response.headers["cache-control"] == "no-store"
    body = second.text
    assert "# TYPE coworker_uptime_seconds gauge" in body
    assert "# TYPE coworker_agent_cycles_total gauge" in body
    assert "coworker_agent_cycles_total 7" in body
    assert 'coworker_messages_in_total{source="wecom"} 1' in body
    assert (
        "coworker_channel_messages_total"
        '{channel="wecom",direction="inbound",status="received"} 1'
    ) in body
    assert "# TYPE coworker_http_requests_total counter" in body
    # 路由标签是模板，而非真实路径。
    assert 'coworker_http_requests_total{method="GET",route="/metrics",status="200"}' in body


def test_metrics_requires_bearer_when_token_is_explicit(client, tmp_path):
    import coworker.api.routes as routes_mod

    registry = MetricsRegistry()
    api_app.setup_metrics_registry(registry)
    _full_setup(tmp_path, registry=registry)
    routes_mod._communication_token = "secret"
    routes_mod._communication_token_explicit = True

    rejected = client.get("/metrics")
    accepted = client.get("/metrics", headers={"Authorization": "Bearer secret"})

    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert "coworker_agent_cycles_total 7" in accepted.text


def test_metrics_redirects_to_admin_during_setup(client, tmp_path):
    registry = MetricsRegistry()
    api_app.setup_metrics_registry(registry)
    _full_setup(tmp_path, registry=registry)
    api_app.set_setup_required(True)

    response = client.get("/metrics", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
