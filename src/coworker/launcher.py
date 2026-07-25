from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence

WINDOWS_RESTART_EXIT_CODE = 75
WINDOWS_SUPERVISOR_PID_ENV = "COWORKER_WINDOWS_SUPERVISOR_PID"
CHILD_INTERRUPT_GRACE_SECONDS = 10
CHILD_TERMINATE_GRACE_SECONDS = 5
ONE_SHOT_ARGUMENTS = frozenset({"--check", "--backfill-tree"})


def main_sync() -> None:
    if _needs_windows_supervisor():
        raise SystemExit(_supervise_windows())

    from coworker.application import run_sync

    run_sync()


def _needs_windows_supervisor() -> bool:
    return (
        sys.platform == "win32"
        and not _is_supervised_worker()
        and not ONE_SHOT_ARGUMENTS.intersection(sys.argv[1:])
    )


def _is_supervised_worker() -> bool:
    supervisor_pid = os.environ.get(WINDOWS_SUPERVISOR_PID_ENV)
    return supervisor_pid == str(os.getppid())


def _supervise_windows() -> int:
    argv = _worker_argv(sys.argv[1:])
    environment = _worker_environment(os.environ)
    while True:
        child = subprocess.Popen(argv, env=environment)
        returncode = _wait_for_child(child)
        if returncode != WINDOWS_RESTART_EXIT_CODE:
            return returncode


def _worker_argv(arguments: Sequence[str]) -> list[str]:
    return [sys.executable, "-m", "coworker", *arguments]


def _worker_environment(environment: Mapping[str, str]) -> dict[str, str]:
    worker_environment = dict(environment)
    worker_environment[WINDOWS_SUPERVISOR_PID_ENV] = str(os.getpid())
    return worker_environment


def _wait_for_child(child: subprocess.Popen) -> int:
    try:
        return child.wait()
    except KeyboardInterrupt:
        _stop_child(child)
        return 130


def _stop_child(child: subprocess.Popen) -> None:
    try:
        child.wait(timeout=CHILD_INTERRUPT_GRACE_SECONDS)
        return
    except (KeyboardInterrupt, subprocess.TimeoutExpired):
        child.terminate()

    try:
        child.wait(timeout=CHILD_TERMINATE_GRACE_SECONDS)
    except (KeyboardInterrupt, subprocess.TimeoutExpired):
        child.kill()
        child.wait()
