from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from coworker.api.admin import router_module as admin
from coworker.core.config import Config, TunnelConfig
from coworker.tunnel import TunnelError, TunnelManager, TunnelTarget, quick_tunnel_url
from coworker.tunnel import manager as tunnel_module


def _config(tmp_path: Path, **tunnel: object) -> Config:
    return Config.model_validate(
        {
            "admin": {
                "token": "secret",
                "config_file": str(tmp_path / "admin_config.json"),
            },
            "api": {"communication_token": "cwct_v1_test", "port": 8000},
            "agent": {"logs_dir": str(tmp_path / "logs")},
            "memory": {"db_path": str(tmp_path / "memory")},
            "tunnel": tunnel,
        }
    )


class _BlockingProcess:
    """Stand-in for a running cloudflared process."""

    pid = os.getpid()
    returncode = None

    async def wait(self) -> int:
        await asyncio.Event().wait()
        return 0


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TunnelManager:
    instance = TunnelManager(_config(tmp_path))

    async def _ensure_binary() -> None:
        return None

    async def _reap_orphan() -> None:
        return None

    async def _terminate() -> None:
        instance._process = None

    monkeypatch.setattr(instance, "_ensure_binary", _ensure_binary)
    monkeypatch.setattr(instance, "_reap_orphan", _reap_orphan)
    monkeypatch.setattr(instance, "_terminate_process", _terminate)
    return instance


async def _wait_for_phase(manager: TunnelManager, phase: str, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if manager.phase == phase:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"tunnel did not reach {phase!r}, stayed at {manager.phase!r}")


# --------------------------------------------------------------------- config


def test_tunnel_config_defaults_to_off() -> None:
    config = TunnelConfig(_env_file=None)
    assert config.mode == "off"
    assert config.protocol == "http2"


def test_named_mode_requires_token_and_public_url() -> None:
    with pytest.raises(ValidationError):
        TunnelConfig(_env_file=None, mode="named", public_url="https://coworker.example.com")
    with pytest.raises(ValidationError):
        TunnelConfig(_env_file=None, mode="named", token="t")


def test_public_url_must_be_https_origin() -> None:
    with pytest.raises(ValidationError):
        TunnelConfig(
            _env_file=None,
            mode="named",
            token="t",
            public_url="http://coworker.example.com",
        )
    with pytest.raises(ValidationError):
        TunnelConfig(
            _env_file=None,
            mode="named",
            token="t",
            public_url="https://coworker.example.com/admin",
        )
    accepted = TunnelConfig(
        _env_file=None,
        mode="named",
        token="t",
        public_url=" https://coworker.example.com/ ",
    )
    assert accepted.public_url == "https://coworker.example.com"


def test_quick_allowed_emails_are_normalized() -> None:
    config = TunnelConfig(
        _env_file=None, mode="quick", quick_allowed_emails=[" me@example.com "]
    )
    assert config.quick_allowed_emails == ["me@example.com"]
    with pytest.raises(ValidationError):
        TunnelConfig(_env_file=None, mode="quick", quick_allowed_emails=["not-an-email"])


# ----------------------------------------------------------------------- parser


def test_quick_url_ignores_the_quick_tunnel_api_endpoint() -> None:
    line = 'ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": dial tcp'
    assert quick_tunnel_url(line) is None


def test_quick_url_reads_the_minted_hostname() -> None:
    line = "INF |  https://demo-ui-check.trycloudflare.com                     |"
    assert quick_tunnel_url(line) == "https://demo-ui-check.trycloudflare.com"


def test_quick_url_normalizes_case_and_ignores_other_hosts() -> None:
    assert (
        quick_tunnel_url("Visit it at HTTPS://Foo-Bar.trycloudflare.com")
        == "https://foo-bar.trycloudflare.com"
    )
    assert quick_tunnel_url("INF https://region1.v2.argotunnel.com") is None


# -------------------------------------------------------------------- lifecycle


async def test_named_tunnel_runs_when_edge_registers_despite_probe_failure(
    manager: TunnelManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _probe(url: str) -> tuple[bool, int, int]:
        return False, 0, 8

    async def _spawn(target: TunnelTarget) -> None:
        manager._process = _BlockingProcess()
        manager._connected_event.set()

    monkeypatch.setattr(manager, "_probe", _probe)
    monkeypatch.setattr(manager, "_spawn", _spawn)

    await manager.connect(
        mode="named", token="tunnel-token", public_url="https://coworker.example.com"
    )
    await _wait_for_phase(manager, "running")

    snapshot = manager.snapshot()
    assert snapshot["phase"] == "running"
    assert snapshot["url"] == "https://coworker.example.com"
    await manager.stop()


async def test_quick_tunnel_runs_when_edge_registers_despite_probe_failure(
    manager: TunnelManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _probe(url: str) -> tuple[bool, int, int]:
        return False, 0, 8

    async def _spawn(target: TunnelTarget) -> None:
        manager._process = _BlockingProcess()
        manager._minted_url = "https://demo.trycloudflare.com"
        manager._url_event.set()
        manager._connected_event.set()

    monkeypatch.setattr(manager, "_probe", _probe)
    monkeypatch.setattr(manager, "_spawn", _spawn)

    await manager.connect(mode="quick")
    await _wait_for_phase(manager, "running")

    assert manager.snapshot()["url"] == "https://demo.trycloudflare.com"
    await manager.stop()


async def test_quick_tunnel_reaches_running(
    manager: TunnelManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _probe(url: str) -> tuple[bool, int, int]:
        return True, 200, 7

    async def _spawn(target: TunnelTarget) -> None:
        manager._process = _BlockingProcess()
        manager._minted_url = "https://demo.trycloudflare.com"
        manager._url_event.set()

    monkeypatch.setattr(manager, "_probe", _probe)
    monkeypatch.setattr(manager, "_spawn", _spawn)

    await manager.connect(mode="quick")
    await _wait_for_phase(manager, "running")

    snapshot = manager.snapshot()
    assert snapshot["mode"] == "quick"
    assert snapshot["url"] == "https://demo.trycloudflare.com"
    assert snapshot["health"]["failures"] == 0

    await manager.stop()
    assert manager.phase == "stopped"
    assert manager.snapshot()["url"] == ""


async def test_spawn_failure_marks_failed_and_keeps_error(
    manager: TunnelManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _spawn(target: TunnelTarget) -> None:
        raise TunnelError("cloudflared exploded")

    monkeypatch.setattr(manager, "_spawn", _spawn)
    await manager.connect(mode="quick")
    await _wait_for_phase(manager, "failed")
    assert manager.snapshot()["error"] == "cloudflared exploded"
    await manager.stop()


async def test_watchdog_restarts_after_two_failed_probes(
    manager: TunnelManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"count": 0}

    async def _probe(url: str) -> tuple[bool, int, int]:
        calls["count"] += 1
        return calls["count"] <= 1, 200 if calls["count"] <= 1 else 0, 5

    async def _spawn(target: TunnelTarget) -> None:
        manager._process = _BlockingProcess()
        manager._minted_url = "https://demo.trycloudflare.com"
        manager._url_event.set()

    monkeypatch.setattr(manager, "_probe", _probe)
    monkeypatch.setattr(manager, "_spawn", _spawn)
    monkeypatch.setattr(tunnel_module, "HEALTH_INTERVAL_SECONDS", 0.01)

    await manager.connect(mode="quick")
    await _wait_for_phase(manager, "running")
    await _wait_for_phase(manager, "failed", timeout=2.0)
    assert "trycloudflare.com" in manager.snapshot()["error"]
    await manager.stop()


async def test_missing_binary_reports_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = TunnelManager(_config(tmp_path))
    monkeypatch.setattr(tunnel_module.shutil, "which", lambda name: None)

    with pytest.raises(TunnelError) as excinfo:
        await manager.connect(mode="quick")
    message = str(excinfo.value)
    assert "cloudflared" in message
    assert "https://github.com/cloudflare/cloudflared/releases" in message


async def test_named_mode_requires_fields(manager: TunnelManager) -> None:
    with pytest.raises(TunnelError):
        await manager.connect(mode="named", public_url="https://coworker.example.com")
    with pytest.raises(TunnelError):
        await manager.connect(mode="named", token="t")


async def test_orphan_record_is_tolerated_when_argv_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = TunnelManager(_config(tmp_path))
    record_path = Path(manager._record_path)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    process = psutil.Process(os.getpid())
    record_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "create_time": process.create_time(),
                "argv": ["definitely", "not", "this", "process"],
            }
        ),
        encoding="utf-8",
    )

    await manager._reap_orphan()
    assert not record_path.exists()
    assert psutil.Process(os.getpid()).is_running()


# ------------------------------------------------------------------------ admin


class _StubTunnelManager:
    def __init__(self, *, url: str = "") -> None:
        self.url = url
        self.stopped = False
        self.refresh_count = 0

    async def refresh_binary(self) -> dict[str, object]:
        self.refresh_count += 1
        return {"found": True}

    def snapshot(self) -> dict[str, object]:
        return {
            "mode": "quick",
            "phase": "starting",
            "url": self.url,
            "public_url": "",
            "error": "",
            "started_at": "",
            "binary": {"found": True, "path": "/usr/bin/cloudflared", "version": "1"},
            "health": {"last_probe_at": "", "failures": 0, "latency_ms": 0},
        }

    async def connect(self, **kwargs: object) -> dict[str, object]:
        return {**self.snapshot(), "mode": kwargs.get("mode")}

    async def stop(self) -> None:
        self.stopped = True

    async def reconnect(self) -> dict[str, object]:
        return self.snapshot()

    async def test(self) -> dict[str, object]:
        return {"url": self.url, "reachable": True, "status": 200, "latency_ms": 3}


def _request() -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))


def test_connect_refuses_without_explicit_communication_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(admin, "_config", config)
    monkeypatch.setattr(admin, "_setup_required", False)
    monkeypatch.setattr(admin, "_tunnel_manager", _StubTunnelManager())

    import coworker.api.routes as routes

    monkeypatch.setattr(routes, "communication_token_required", lambda: False)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            admin.connect_tunnel(
                admin.TunnelConnectPayload(mode="quick"), _request(), None
            )
        )
    assert excinfo.value.status_code == 409
    assert "API__COMMUNICATION_TOKEN" in str(excinfo.value.detail)


def test_connect_persists_tunnel_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(admin, "_config", config)
    monkeypatch.setattr(admin, "_setup_required", False)
    monkeypatch.setattr(admin, "_tunnel_manager", _StubTunnelManager())

    import coworker.api.routes as routes

    monkeypatch.setattr(routes, "communication_token_required", lambda: True)

    result = asyncio.run(
        admin.connect_tunnel(
            admin.TunnelConnectPayload(
                mode="named",
                token="tunnel-token",
                public_url="https://coworker.example.com",
            ),
            _request(),
            None,
        )
    )
    assert result["mode"] == "named"
    saved = json.loads(Path(config.admin.config_file).read_text(encoding="utf-8"))
    assert saved["tunnel"]["mode"] == "named"
    assert saved["tunnel"]["public_url"] == "https://coworker.example.com"
    assert saved["tunnel"]["token"] == "tunnel-token"


def test_disconnect_clears_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, mode="named", token="t", public_url="https://x.example.com")
    stub = _StubTunnelManager()
    monkeypatch.setattr(admin, "_config", config)
    monkeypatch.setattr(admin, "_tunnel_manager", stub)

    response = asyncio.run(admin.disconnect_tunnel(_request(), None))
    assert response.status_code == 204
    assert stub.stopped is True
    saved = json.loads(Path(config.admin.config_file).read_text(encoding="utf-8"))
    assert saved["tunnel"]["mode"] == "off"
    assert "token" not in saved["tunnel"]


def test_qr_endpoint_returns_png(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        admin,
        "_tunnel_manager",
        _StubTunnelManager(url="https://demo.trycloudflare.com"),
    )
    response = asyncio.run(admin.tunnel_qr(None))
    assert response.media_type == "image/png"
    assert response.body.startswith(b"\x89PNG")


def test_qr_endpoint_requires_connected_tunnel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(admin, "_tunnel_manager", _StubTunnelManager())
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(admin.tunnel_qr(None))
    assert excinfo.value.status_code == 404


async def test_manager_test_probe_reports_unreachable(
    manager: TunnelManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _probe(url: str) -> tuple[bool, int, int]:
        return False, 0, 12

    monkeypatch.setattr(manager, "_probe", _probe)
    manager._url = "https://demo.trycloudflare.com"
    result = await manager.test()
    assert result["reachable"] is False
    assert result["status"] == 0


def test_snapshot_reports_configuration_without_leaking_token(
    manager: TunnelManager,
) -> None:
    manager._config.tunnel.mode = "named"
    manager._config.tunnel.public_url = "https://coworker.example.com"
    manager._config.tunnel.token = "secret-token"

    snapshot = manager.snapshot()
    assert snapshot["configured"] is True
    assert snapshot["token_configured"] is True
    assert "secret-token" not in json.dumps(snapshot)


async def test_refresh_binary_detects_configured_path(tmp_path: Path) -> None:
    manager = TunnelManager(_config(tmp_path, binary=sys.executable))

    info = await manager.refresh_binary()
    assert info.found is True
    snapshot = manager.snapshot()
    assert snapshot["binary"]["found"] is True
    assert snapshot["binary"]["checked"] is True


async def test_refresh_binary_reports_missing_then_recovers(tmp_path: Path) -> None:
    manager = TunnelManager(_config(tmp_path, binary=str(tmp_path / "missing")))

    info = await manager.refresh_binary()
    assert info.found is False
    manager._config.tunnel.binary = sys.executable
    info = await manager.refresh_binary()
    assert info.found is True


def test_tunnel_status_refreshes_binary_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _StubTunnelManager()
    monkeypatch.setattr(admin, "_config", _config(tmp_path))
    monkeypatch.setattr(admin, "_tunnel_manager", stub)

    import coworker.api.routes as routes

    monkeypatch.setattr(routes, "communication_token_required", lambda: True)

    result = asyncio.run(admin.get_tunnel(None))

    assert stub.refresh_count == 1
    assert result["prerequisites"] == {
        "communication_token": True,
        "setup_complete": True,
    }


async def test_connect_named_reuses_saved_token(
    manager: TunnelManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager._config.tunnel.public_url = "https://coworker.example.com"
    manager._config.tunnel.token = "saved-token"

    async def _spawn(target: TunnelTarget) -> None:
        manager._process = _BlockingProcess()

    async def _probe(url: str) -> tuple[bool, int, int]:
        return True, 200, 3

    monkeypatch.setattr(manager, "_spawn", _spawn)
    monkeypatch.setattr(manager, "_probe", _probe)

    await manager.connect(mode="named", token="", public_url="")
    target = manager._target
    assert target is not None
    assert target.token == "saved-token"
    assert target.public_url == "https://coworker.example.com"
    await manager.stop()
