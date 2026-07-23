from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

import coworker.__main__ as main_module


def _forbid_windows_ctrl_c_ignore(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_set_console_ctrl_handler(*_args):
        raise AssertionError("_exec_replace must not make Ctrl-C ignored/inherited")

    monkeypatch.setitem(
        sys.modules,
        "ctypes",
        SimpleNamespace(
            windll=SimpleNamespace(
                kernel32=SimpleNamespace(SetConsoleCtrlHandler=fail_set_console_ctrl_handler)
            )
        ),
    )


def test_windows_exec_replace_does_not_disable_ctrl_c_for_child(monkeypatch: pytest.MonkeyPatch):
    _forbid_windows_ctrl_c_ignore(monkeypatch)
    monkeypatch.setattr(main_module.sys, "platform", "win32")
    monkeypatch.setattr(main_module.sys, "executable", "python.exe")
    monkeypatch.setattr(main_module.sys, "argv", ["coworker", "--flag"])

    waits: list[float | None] = []
    popen_argv: list[list[str]] = []

    class Proc:
        returncode = 7

        def wait(self, timeout: float | None = None) -> int:
            waits.append(timeout)
            return self.returncode

    def fake_popen(argv: list[str]) -> Proc:
        popen_argv.append(argv)
        return Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(SystemExit) as exc:
        main_module._exec_replace()

    assert exc.value.code == 7
    assert popen_argv == [["python.exe", "-m", "coworker", "--flag"]]
    assert waits == [None]


def test_windows_exec_replace_waits_for_child_after_ctrl_c(monkeypatch: pytest.MonkeyPatch):
    _forbid_windows_ctrl_c_ignore(monkeypatch)
    monkeypatch.setattr(main_module.sys, "platform", "win32")
    monkeypatch.setattr(main_module.sys, "executable", "python.exe")
    monkeypatch.setattr(main_module.sys, "argv", ["coworker"])

    waits: list[float | None] = []

    class Proc:
        returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            waits.append(timeout)
            if len(waits) == 1:
                raise KeyboardInterrupt
            return self.returncode

        def terminate(self) -> None:  # pragma: no cover - should not be needed here
            raise AssertionError("child exited after Ctrl-C; terminate should not be called")

        def kill(self) -> None:  # pragma: no cover - should not be needed here
            raise AssertionError("child exited after Ctrl-C; kill should not be called")

    monkeypatch.setattr(subprocess, "Popen", lambda _argv: Proc())

    with pytest.raises(SystemExit) as exc:
        main_module._exec_replace()

    assert exc.value.code == 130
    assert waits == [None, 10]
