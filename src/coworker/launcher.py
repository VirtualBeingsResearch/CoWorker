from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

WINDOWS_WORKER_ENV = "COWORKER_INTERNAL_WINDOWS_WORKER"
WINDOWS_DESCENDANT_MARKER = "descendant"
CHILD_INTERRUPT_GRACE_SECONDS = 10
CHILD_TERMINATE_GRACE_SECONDS = 5


def main_sync() -> None:
    worker_context = os.environ.get(WINDOWS_WORKER_ENV)
    if sys.platform == "win32" and not worker_context:
        raise SystemExit(_supervise_windows())
    restart_signal = None if worker_context == WINDOWS_DESCENDANT_MARKER else worker_context
    if worker_context:
        os.environ[WINDOWS_WORKER_ENV] = WINDOWS_DESCENDANT_MARKER

    from coworker.application import run_sync

    run_sync(restart_signal)


def _supervise_windows() -> int:
    argv = [sys.executable, *sys.orig_argv[1:]]
    environment = dict(os.environ)
    restart_signal = Path(
        tempfile.gettempdir(), f"coworker-restart-{os.getpid()}.signal"
    )
    environment[WINDOWS_WORKER_ENV] = str(restart_signal)
    try:
        while True:
            restart_signal.unlink(missing_ok=True)
            with subprocess.Popen(argv, env=environment) as child:
                try:
                    returncode = child.wait()
                except KeyboardInterrupt:
                    _stop_child(child)
                    return 130
            if not restart_signal.exists():
                return returncode
    finally:
        restart_signal.unlink(missing_ok=True)


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
