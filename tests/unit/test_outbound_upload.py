"""收口 UploadTracker 创建的后台任务。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

import pytest

from coworker.channels.outbound_upload import UploadTracker


@pytest.mark.asyncio
async def test_drain_waits_for_a_running_upload() -> None:
    tracker = UploadTracker()
    finished = asyncio.Event()

    async def upload() -> None:
        await asyncio.sleep(0.01)
        finished.set()

    tracker.schedule(upload, name="probe")

    await tracker.drain()

    assert finished.is_set()


def test_drain_finishes_when_a_tracked_task_already_completed() -> None:
    """任务已经跑完、收尾回调还没轮到，也必须能收口。

    旧实现 ``while self._tasks: await gather(...)`` 对这种任务会立即返回、不再
    交还事件循环，于是死循环卡住整个进程。这里把它放到独立线程里跑，用超时暴露
    卡死，而不是让测试跟着挂住。
    """

    async def scenario() -> None:
        tracker = UploadTracker()

        async def upload() -> None:
            return None

        tracker.schedule(upload, name="probe")
        await asyncio.sleep(0)
        # 前提：任务已结束，但 done 回调还没执行，所以它仍在跟踪集合里。
        assert tracker._tasks
        assert all(task.done() for task in tracker._tasks)

        await tracker.drain()

        assert tracker._tasks == set()

    _run_in_worker(lambda: asyncio.run(scenario()), timeout=5.0)


def _run_in_worker(
    run: Callable[[], None],
    *,
    timeout: float,
) -> None:
    """在后台线程里跑一个会创建事件循环的调用，卡死时报超时而不是挂住测试。"""
    errors: list[BaseException] = []

    def target() -> None:
        try:
            run()
        except BaseException as error:  # 失败原因交回主线程断言
            errors.append(error)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout)

    assert not worker.is_alive(), f"drain() 没有在 {timeout} 秒内返回：事件循环被卡住了"
    if errors:
        raise errors[0]
