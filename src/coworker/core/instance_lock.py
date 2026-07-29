"""Cross-platform single-instance lock for one Coworker data directory."""

from __future__ import annotations

import errno
import os
from pathlib import Path


class InstanceAlreadyRunningError(RuntimeError):
    pass


class InstanceLock:
    """Hold an advisory process lock without trusting a stale PID file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._descriptor: int | None = None

    def acquire(self) -> None:
        if self._descriptor is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                getattr(msvcrt, "locking")(
                    descriptor,
                    getattr(msvcrt, "LK_NBLCK"),
                    1,
                )
            else:
                import fcntl

                os.fchmod(descriptor, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise InstanceAlreadyRunningError(str(self.path)) from error
            raise
        self._descriptor = descriptor

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                getattr(msvcrt, "locking")(
                    descriptor,
                    getattr(msvcrt, "LK_UNLCK"),
                    1,
                )
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.release()
