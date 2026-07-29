from __future__ import annotations

import os

import pytest

from coworker.core.instance_lock import (
    InstanceAlreadyRunningError,
    InstanceLock,
)


def test_instance_lock_rejects_second_owner_and_can_be_reacquired(tmp_path):
    path = tmp_path / "coworker.instance.lock"
    first = InstanceLock(path)
    second = InstanceLock(path)

    first.acquire()
    try:
        with pytest.raises(InstanceAlreadyRunningError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission assertion")
def test_instance_lock_is_private_from_creation(tmp_path):
    path = tmp_path / "coworker.instance.lock"

    with InstanceLock(path):
        assert path.stat().st_mode & 0o777 == 0o600
