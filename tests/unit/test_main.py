from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from coworker import application, launcher


def test_windows_supervisor_replaces_worker_without_nesting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_argv = [
        "base-python.exe",
        "-X",
        "utf8",
        "-m",
        "coworker",
        "status",
        "--json",
    ]
    monkeypatch.setattr(launcher.sys, "executable", "venv-python.exe")
    monkeypatch.setattr(launcher.sys, "orig_argv", original_argv)

    children: list[tuple[list[str], dict[str, str]]] = []
    returncodes = iter([launcher.WINDOWS_RESTART_EXIT_CODE, 7])

    class Child:
        def wait(self, timeout: float | None = None) -> int:
            assert timeout is None
            return next(returncodes)

    def fake_popen(argv: list[str], *, env: dict[str, str]) -> Child:
        children.append((argv, env))
        return Child()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    assert launcher._supervise_windows() == 7
    expected_argv = ["venv-python.exe", *original_argv[1:]]
    assert [argv for argv, _ in children] == [expected_argv, expected_argv]
    assert all(
        environment[launcher.WINDOWS_WORKER_ENV] == launcher.WINDOWS_WORKER_TOKEN
        for _, environment in children
    )


def test_windows_worker_token_runs_application_without_nested_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application_runs: list[bool] = []
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setenv(launcher.WINDOWS_WORKER_ENV, launcher.WINDOWS_WORKER_TOKEN)

    def fail_supervisor_start() -> int:
        raise AssertionError("worker must not start a nested supervisor")

    monkeypatch.setattr(launcher, "_supervise_windows", fail_supervisor_start)
    monkeypatch.setitem(
        sys.modules,
        "coworker.application",
        SimpleNamespace(run_sync=lambda: application_runs.append(True)),
    )

    launcher.main_sync()

    assert application_runs == [True]
    assert launcher.WINDOWS_WORKER_ENV not in os.environ


def test_windows_launch_without_worker_token_runs_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher.sys, "argv", ["coworker"])
    monkeypatch.delenv(launcher.WINDOWS_WORKER_ENV, raising=False)
    monkeypatch.setattr(launcher, "_supervise_windows", lambda: 7)

    with pytest.raises(SystemExit) as exc:
        launcher.main_sync()

    assert exc.value.code == 7


def test_windows_supervisor_stops_child_after_ctrl_c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float | None] = []

    class Child:
        def wait(self, timeout: float | None = None) -> int:
            waits.append(timeout)
            if len(waits) == 1:
                raise KeyboardInterrupt
            return 0

        def terminate(self) -> None:
            raise AssertionError("child exited during grace period")

        def kill(self) -> None:
            raise AssertionError("child exited during grace period")

    monkeypatch.setattr(subprocess, "Popen", lambda _argv, *, env: Child())

    assert launcher._supervise_windows() == 130
    assert waits == [None, launcher.CHILD_INTERRUPT_GRACE_SECONDS]


def test_windows_supervisor_kills_unresponsive_child() -> None:
    actions: list[str] = []

    class Child:
        def wait(self, timeout: float | None = None) -> int:
            actions.append(f"wait:{timeout}")
            if timeout is not None:
                raise subprocess.TimeoutExpired("coworker", timeout)
            return 1

        def terminate(self) -> None:
            actions.append("terminate")

        def kill(self) -> None:
            actions.append("kill")

    launcher._stop_child(Child())

    assert actions == [
        f"wait:{launcher.CHILD_INTERRUPT_GRACE_SECONDS}",
        "terminate",
        f"wait:{launcher.CHILD_TERMINATE_GRACE_SECONDS}",
        "kill",
        "wait:None",
    ]


def test_windows_restart_hard_exits_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(application.sys, "platform", "win32")

    def fake_exit(returncode: int) -> None:
        raise SystemExit(returncode)

    monkeypatch.setattr(os, "_exit", fake_exit)

    with pytest.raises(SystemExit) as exc:
        application._restart_process()

    assert exc.value.code == launcher.WINDOWS_RESTART_EXIT_CODE
