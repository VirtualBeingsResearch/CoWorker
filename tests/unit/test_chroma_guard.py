from __future__ import annotations

import threading
import time

from coworker.memory.chroma_guard import (
    _ChromaClientProxy,
    _ChromaCollectionProxy,
    guarded_chroma_client,
    guarded_chroma_collection,
)


class _CountingCollection:
    """Fake chroma collection that records every call."""

    def __init__(self) -> None:
        self.adds: list[tuple] = []
        self.queries: list[tuple] = []
        self.name = "memories"

    def add(self, ids=None, embeddings=None, metadatas=None):
        self.adds.append((ids, embeddings, metadatas))
        return True

    def query(self, query_embeddings=None, n_results=5):
        self.queries.append((query_embeddings, n_results))
        return {"ids": [[1]], "documents": [[]], "metadatas": [[]]}


class _FakeClient:
    """Fake chroma client exposing a collection-returning method."""

    def __init__(self, collection=None) -> None:
        self._collection = collection or _CountingCollection()

    def get_or_create_collection(self, name, embedding_function=None):
        self._collection.name = name
        return self._collection


def test_collection_proxy_delegates_calls_and_passthroughs_attributes():
    inner = _CountingCollection()
    proxy = guarded_chroma_collection(inner)

    # Plain attributes pass through unwrapped.
    assert proxy.name == "memories"

    # Callable attributes reach the inner object and return its result.
    assert proxy.add(ids=["a"], embeddings=[[1.0]]) is True
    assert inner.adds == [(["a"], [[1.0]], None)]

    assert proxy.query(query_embeddings=[[2.0]], n_results=3)["ids"] == [[1]]
    assert inner.queries == [([[2.0]], 3)]


def test_client_proxy_wraps_returned_collections():
    inner = _CountingCollection()
    proxy = guarded_chroma_client(_FakeClient(inner))

    collection = proxy.get_or_create_collection("memories")
    assert isinstance(collection, _ChromaCollectionProxy)

    collection.add(ids=["a"], embeddings=[[1.0]])
    assert inner.adds == [(["a"], [[1.0]], None)]


def test_client_proxy_is_exposed_as_proxy_instance():
    inner = _FakeClient()
    proxy = guarded_chroma_client(inner)
    assert isinstance(proxy, _ChromaClientProxy)


def test_concurrent_calls_are_serialized_by_the_shared_lock():
    """Two threads entering the same guarded operation never overlap."""
    inner = _CountingCollection()
    proxy = guarded_chroma_collection(inner)

    state = {"active": 0, "max_active": 0}
    guard = threading.Lock()

    original = inner.add

    def tracking_add(*args, **kwargs):
        with guard:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.02)
        with guard:
            state["active"] -= 1
        return original(*args, **kwargs)

    inner.add = tracking_add

    threads = [threading.Thread(target=proxy.add) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert state["max_active"] == 1
    assert len(inner.adds) == 8


def test_lock_wait_warning_logs_when_over_threshold():
    from loguru import logger

    import coworker.memory.chroma_guard as chroma_guard_module

    records = []
    sink_id = logger.add(lambda message: records.append(message.record.copy()), level="WARNING")
    try:
        # A negative threshold makes any wait (even ~0 ms) exceed it.
        fn = chroma_guard_module._make_locked_callable(lambda: "ok", warn_ms=-1.0)
        assert fn() == "ok"
    finally:
        logger.remove(sink_id)

    warnings = [r for r in records if r["message"].startswith("Slow Chroma lock wait")]
    assert warnings
    assert "waited" in warnings[0]["message"]


def test_lock_wait_measured_under_contention():
    from loguru import logger

    import coworker.memory.chroma_guard as chroma_guard_module

    records = []
    sink_id = logger.add(lambda message: records.append(message.record.copy()), level="WARNING")
    entered = threading.Event()
    release = threading.Event()
    result: dict[str, object] = {}

    def holder() -> None:
        with chroma_guard_module._LOCK:
            entered.set()
            release.wait(5)

    def call() -> None:
        fn = chroma_guard_module._make_locked_callable(lambda: "ok", warn_ms=1.0)
        result["value"] = fn()

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()
    assert entered.wait(5)
    # Release the lock shortly after the caller starts waiting, so the measured
    # wait (~50 ms) is comfortably above the 1 ms threshold.
    timer = threading.Timer(0.05, release.set)
    timer.start()
    try:
        call_thread = threading.Thread(target=call)
        call_thread.start()
        call_thread.join(5)
        timer.join(5)
    finally:
        release.set()
        holder_thread.join(5)
        logger.remove(sink_id)

    assert result.get("value") == "ok"
    warnings = [r for r in records if r["message"].startswith("Slow Chroma lock wait")]
    assert warnings
    measured_ms = float(warnings[0]["message"].split("waited ")[1].split("ms")[0])
    assert measured_ms > 1
