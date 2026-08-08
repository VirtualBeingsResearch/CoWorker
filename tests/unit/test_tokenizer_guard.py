from __future__ import annotations

import threading
import time

from coworker.memory.tokenizer_guard import (
    _TokenizerProxy,
    guarded_tokenizer,
)


class _FakeTokenizer:
    """Fake ``tokenizers.Tokenizer`` backend recording every call.

    Mirrors the interesting surface of the real Rust object: callable
    operations (``encode_batch``, ``enable_truncation``) plus plain attribute
    reads (``truncation``, ``padding``) that the transformers wrapper reads on
    every call.
    """

    def __init__(self) -> None:
        self.encodes: list[tuple] = []
        self.truncations: list[tuple] = []
        self.truncation = None
        self.padding = None

    def encode_batch(self, inputs, add_special_tokens=True):
        self.encodes.append((inputs, add_special_tokens))
        return [len(i) for i in inputs]

    def enable_truncation(self, max_length, stride=0, strategy="longest_first", direction="right"):
        self.truncations.append((max_length, stride, strategy, direction))
        self.truncation = {"max_length": max_length}
        return None


def test_tokenizer_proxy_delegates_calls_and_passthroughs_attributes():
    inner = _FakeTokenizer()
    proxy = guarded_tokenizer(inner)

    # Plain attributes pass through unwrapped.
    assert proxy.truncation is None

    # Callable attributes reach the inner object and return its result.
    assert proxy.encode_batch(["a", "bb"]) == [1, 2]
    assert inner.encodes == [(["a", "bb"], True)]

    proxy.enable_truncation(max_length=128)
    assert inner.truncations == [(128, 0, "longest_first", "right")]


def test_tokenizer_proxy_is_exposed_as_proxy_instance():
    inner = _FakeTokenizer()
    proxy = guarded_tokenizer(inner)
    assert isinstance(proxy, _TokenizerProxy)


def test_concurrent_calls_are_serialized_by_the_shared_lock():
    """Two threads entering the same guarded tokenizer call never overlap."""
    inner = _FakeTokenizer()
    proxy = guarded_tokenizer(inner)

    state = {"active": 0, "max_active": 0}
    guard = threading.Lock()

    original = inner.encode_batch

    def tracking_encode(*args, **kwargs):
        with guard:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.02)
        with guard:
            state["active"] -= 1
        return original(*args, **kwargs)

    inner.encode_batch = tracking_encode

    threads = [threading.Thread(target=proxy.encode_batch, args=(["x"],)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert state["max_active"] == 1
    assert len(inner.encodes) == 8


def test_exclusive_borrow_waits_for_batch_encode():
    """A truncation setter (exclusive borrow) serializes behind a batch encode.

    This mirrors the production failure: ``encode_batch`` holds a shared borrow
    while releasing the GIL, and a concurrent ``enable_truncation`` needs an
    exclusive borrow. Under the guard the setter waits instead of raising
    ``RuntimeError: Already borrowed``.
    """
    inner = _FakeTokenizer()
    proxy = guarded_tokenizer(inner)

    in_batch = threading.Event()
    release_batch = threading.Event()
    results: dict[str, object] = {}

    original_encode = inner.encode_batch

    def slow_encode_batch(inputs, add_special_tokens=True):
        in_batch.set()
        release_batch.wait(5)
        return original_encode(inputs, add_special_tokens)

    inner.encode_batch = slow_encode_batch

    def batch_encoder() -> None:
        try:
            proxy.encode_batch(["long document"])
            results["batch"] = "ok"
        except Exception as e:  # pragma: no cover - only on regression
            results["batch"] = f"{type(e).__name__}: {e}"

    def truncation_setter() -> None:
        try:
            proxy.enable_truncation(max_length=128)
            results["setter"] = "ok"
        except Exception as e:  # pragma: no cover - only on regression
            results["setter"] = f"{type(e).__name__}: {e}"

    batch_thread = threading.Thread(target=batch_encoder)
    batch_thread.start()
    assert in_batch.wait(5)
    setter_thread = threading.Thread(target=truncation_setter)
    setter_thread.start()
    time.sleep(0.05)  # let the setter block on the lock
    release_batch.set()
    batch_thread.join(5)
    setter_thread.join(5)

    assert results == {"batch": "ok", "setter": "ok"}
    assert inner.truncations == [(128, 0, "longest_first", "right")]


def test_lock_wait_warning_logs_when_over_threshold():
    from loguru import logger

    import coworker.memory.tokenizer_guard as tokenizer_guard_module

    records = []
    sink_id = logger.add(lambda message: records.append(message.record.copy()), level="WARNING")
    try:
        fn = tokenizer_guard_module._make_locked_callable(lambda: "ok")
        assert fn() == "ok"
    finally:
        logger.remove(sink_id)

    # No wait -> no warning at the default threshold.
    assert not [r for r in records if r["message"].startswith("Slow tokenizer lock wait")]


def test_lock_wait_warning_fires_under_contention():
    from loguru import logger

    import coworker.memory.tokenizer_guard as tokenizer_guard_module

    records = []
    sink_id = logger.add(lambda message: records.append(message.record.copy()), level="WARNING")
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with tokenizer_guard_module._LOCK:
            entered.set()
            release.wait(5)

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()
    assert entered.wait(5)

    fn = tokenizer_guard_module._make_locked_callable(lambda: "ok", warn_ms=1.0)
    result = []
    timer = threading.Timer(0.05, release.set)
    timer.start()
    try:
        caller = threading.Thread(target=lambda: result.append(fn()))
        caller.start()
        caller.join(5)
        timer.join(5)
    finally:
        release.set()
        holder_thread.join(5)
        logger.remove(sink_id)

    assert result == ["ok"]
    warnings = [r for r in records if r["message"].startswith("Slow tokenizer lock wait")]
    assert warnings
    assert "waited" in warnings[0]["message"]
