from __future__ import annotations

import os
import subprocess
import sys

WINDOWS_RESTART_EXIT_CODE = 75
WINDOWS_WORKER_ENV = "COWORKER_INTERNAL_WINDOWS_WORKER"
WINDOWS_WORKER_TOKEN = "1"
CHILD_INTERRUPT_GRACE_SECONDS = 10
CHILD_TERMINATE_GRACE_SECONDS = 5


def main_sync() -> None:
    is_windows_worker = (
        os.environ.pop(WINDOWS_WORKER_ENV, None) == WINDOWS_WORKER_TOKEN
    )
    if sys.platform == "win32" and not is_windows_worker:
        raise SystemExit(_supervise_windows())

    from coworker.application import run_sync

    run_sync()


def _supervise_windows() -> int:
    argv = [sys.executable, *sys.orig_argv[1:]]
    environment = dict(os.environ)
    environment[WINDOWS_WORKER_ENV] = WINDOWS_WORKER_TOKEN
    while True:
        child = subprocess.Popen(argv, env=environment)
        try:
            returncode = child.wait()
        except KeyboardInterrupt:
            _stop_child(child)
            return 130
        if returncode != WINDOWS_RESTART_EXIT_CODE:
            return returncode


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
