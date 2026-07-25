from __future__ import annotations

import os
import subprocess

import pytest

from coworker import application, launcher


def test_windows_supervisor_replaces_worker_without_nesting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher.sys, "executable", "python.exe")
    monkeypatch.setattr(launcher.sys, "argv", ["coworker", "--flag"])
    monkeypatch.setattr(launcher.os, "getpid", lambda: 42)

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
    assert [argv for argv, _ in children] == [
        ["python.exe", "-m", "coworker", "--flag"],
        ["python.exe", "-m", "coworker", "--flag"],
    ]
    assert all(
        environment[launcher.WINDOWS_SUPERVISOR_PID_ENV] == "42"
        for _, environment in children
    )


def test_windows_worker_requires_its_direct_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher.os, "getppid", lambda: 42)
    monkeypatch.setenv(launcher.WINDOWS_SUPERVISOR_PID_ENV, "42")

    assert launcher._needs_windows_supervisor() is False


def test_inherited_supervisor_marker_starts_a_new_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher.os, "getppid", lambda: 99)
    monkeypatch.setenv(launcher.WINDOWS_SUPERVISOR_PID_ENV, "42")

    assert launcher._needs_windows_supervisor() is True


def test_windows_one_shot_command_skips_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher.sys, "argv", ["coworker", "--check"])
    monkeypatch.delenv(launcher.WINDOWS_SUPERVISOR_PID_ENV, raising=False)

    assert launcher._needs_windows_supervisor() is False


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

    assert launcher._wait_for_child(Child()) == 130
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
