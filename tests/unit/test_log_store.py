from __future__ import annotations

import json
from datetime import datetime

from coworker.agent.interaction_log import InteractionLogger
from coworker.agent.log_store import LogStore


def _write_shard(path, entries: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )


def _entry(seq: int, minute: int, type_: str = "message_in", **extra) -> dict:
    e = {"type": type_, "seq": seq, "ts": f"2026-06-07T09:{minute:02d}:00"}
    e.update(extra)
    return e


class TestInteractionLogSeq:
    def test_seq_monotonic_and_persists_across_restart(self, tmp_path):
        p = tmp_path / "interactions.jsonl"
        log = InteractionLogger(str(p))
        log.log_message_in("alice", "hi", "ws")
        log.log_message_in("bob", "yo", "ws")
        assert log.last_seq() == 2

        # 新进程：从 sidecar 续号，不重置
        log2 = InteractionLogger(str(p))
        assert log2.last_seq() == 2
        log2.log_message_in("carol", "z", "ws")

        lines = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert [e["seq"] for e in lines] == [0, 1, 2]
        assert all("_log_offset" not in e and "_log_path" not in e for e in lines)

    def test_mem0_usage_source_is_logged(self, tmp_path):
        p = tmp_path / "interactions.jsonl"
        log = InteractionLogger(str(p))

        log.log_mem0_llm_response(
            provider="mock",
            model="mem-model",
            usage={"input_tokens": 1, "output_tokens": 2},
            usage_source="estimated",
            operation="generate_response",
        )

        entry = json.loads(p.read_text(encoding="utf-8").strip())
        assert entry["type"] == "mem0_llm_response"
        assert entry["usage_source"] == "estimated"
        assert entry["operation"] == "generate_response"


class TestInteractionLogRotation:
    def test_rotates_by_size_and_log_store_reads_full_history(self, tmp_path):
        path = tmp_path / "interactions.jsonl"
        log = InteractionLogger(str(path), rotation_bytes=300)

        for content in ("a" * 100, "b" * 100, "c" * 100):
            log.log_message_in("alice", content, "ws")

        assert sorted(p.name for p in tmp_path.glob("interactions*.jsonl")) == [
            "interactions-000001.jsonl",
            "interactions-000002.jsonl",
            "interactions.jsonl",
        ]
        assert [
            json.loads(line)["seq"]
            for line in (tmp_path / "interactions-000001.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ] == [0]
        assert [
            json.loads(line)["seq"]
            for line in (tmp_path / "interactions-000002.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ] == [1]
        assert [
            json.loads(line)["seq"] for line in path.read_text(encoding="utf-8").splitlines()
        ] == [2]

        entries, complete = LogStore(tmp_path).read_seq_range(0, 2)
        assert complete is True
        assert [entry["seq"] for entry in entries] == [0, 1, 2]

    def test_rotation_uses_next_unused_archive_number(self, tmp_path):
        path = tmp_path / "interactions.jsonl"
        old_archive = tmp_path / "interactions-000001.jsonl"
        old_archive.write_text("keep this archive\n", encoding="utf-8")
        path.write_text("legacy active log\n", encoding="utf-8")

        log = InteractionLogger(str(path), rotation_bytes=1)
        log.log_message_in("alice", "new entry", "ws")

        assert old_archive.read_text(encoding="utf-8") == "keep this archive\n"
        assert (tmp_path / "interactions-000002.jsonl").read_text(encoding="utf-8") == (
            "legacy active log\n"
        )

    def test_zero_rotation_bytes_keeps_legacy_single_file_behavior(self, tmp_path):
        path = tmp_path / "interactions.jsonl"
        log = InteractionLogger(str(path), rotation_bytes=0)

        for _ in range(3):
            log.log_message_in("alice", "entry", "ws")

        assert sorted(p.name for p in tmp_path.glob("interactions-*.jsonl")) == []
        assert len(path.read_text(encoding="utf-8").splitlines()) == 3


class TestLogStoreRead:
    def test_read_seq_range_across_two_shards(self, tmp_path):
        _write_shard(tmp_path / "interactions.jsonl", [
            _entry(0, 0, content="a"), _entry(1, 1, content="b"), _entry(2, 2, content="c"),
        ])
        _write_shard(tmp_path / "interactions-000001.jsonl", [
            _entry(3, 3, content="d"), _entry(4, 4, content="e"), _entry(5, 5, content="f"),
        ])
        store = LogStore(tmp_path)
        entries, complete = store.read_seq_range(1, 4)
        assert complete is True
        assert [e["seq"] for e in entries] == [1, 2, 3, 4]

    def test_manifest_sorted_by_seq_min(self, tmp_path):
        _write_shard(tmp_path / "interactions-000001.jsonl", [_entry(3, 3), _entry(5, 5)])
        _write_shard(tmp_path / "interactions.jsonl", [_entry(0, 0), _entry(2, 2)])
        store = LogStore(tmp_path)
        shards = store.manifest()
        assert [s.seq_min for s in shards] == [0, 3]

    def test_read_time_range_filters_by_ts(self, tmp_path):
        _write_shard(tmp_path / "interactions.jsonl", [
            _entry(0, 0, content="a"), _entry(1, 5, content="b"), _entry(2, 10, content="c"),
        ])
        store = LogStore(tmp_path)
        entries, _ = store.read_time_range(
            datetime(2026, 6, 7, 9, 3, 0), datetime(2026, 6, 7, 9, 7, 0)
        )
        assert [e["seq"] for e in entries] == [1]

    def test_read_recent_days_filters_by_window(self, tmp_path):
        _write_shard(tmp_path / "interactions.jsonl", [
            {"type": "message_in", "seq": 0, "ts": "2026-06-05T09:00:00", "content": "old"},
            {"type": "message_in", "seq": 1, "ts": "2026-06-10T09:00:00", "content": "recent"},
            {"type": "message_in", "seq": 2, "ts": "2026-06-11T09:00:00", "content": "now"},
            {"type": "message_in", "seq": 3, "ts": "2026-06-12T09:00:00", "content": "future"},
        ])
        store = LogStore(tmp_path)
        entries, complete = store.read_recent_days(2, now=datetime(2026, 6, 11, 12, 0, 0))
        assert complete is True
        assert [e["seq"] for e in entries] == [1, 2]

    def test_read_tail_reads_last_lines_across_shards(self, tmp_path):
        _write_shard(tmp_path / "interactions.jsonl", [
            _entry(0, 0, content="a"),
            _entry(1, 1, content="b"),
            _entry(2, 2, content="c"),
        ])
        _write_shard(tmp_path / "interactions-000001.jsonl", [
            _entry(3, 3, content="d"),
            _entry(4, 4, content="e"),
            _entry(5, 5, content="f"),
        ])
        store = LogStore(tmp_path)
        entries, complete = store.read_tail(4)
        assert complete is True
        assert [e["seq"] for e in entries] == [2, 3, 4, 5]

    def test_iter_entries_after_uses_seq_checkpoint(self, tmp_path):
        path = tmp_path / "interactions.jsonl"
        _write_shard(path, [_entry(0, 0, content="a"), _entry(1, 1, content="b")])
        store = LogStore(tmp_path)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(_entry(2, 2, content="c"), ensure_ascii=False) + "\n")

        entries = list(store.iter_entries_after(1))

        assert [e["seq"] for e in entries] == [2]

    def test_digest_includes_conversational_entries(self, tmp_path):
        _write_shard(tmp_path / "interactions.jsonl", [
            _entry(0, 0, type_="message_in", participant_id="alice", content="部署上线了吗"),
            _entry(1, 1, type_="tool_result", name="run", content="ok", is_error=False),
            _entry(2, 2, type_="system_prompt", content="巨大的系统提示词" * 100),
        ])
        store = LogStore(tmp_path)
        digest = store.digest_seq_range(0, 2)
        assert digest is not None
        assert "alice" in digest and "部署上线了吗" in digest
        assert "run" in digest
        # system_prompt 不进 digest（噪声）
        assert "巨大的系统提示词" not in digest

    def test_digest_none_for_empty_range(self, tmp_path):
        _write_shard(tmp_path / "interactions.jsonl", [_entry(0, 0, content="a")])
        store = LogStore(tmp_path)
        # 完全早于全部数据的时间窗 → 窗内无记录 → digest None（recall 下钻据此提示「无记录」）
        digest, _complete = store.recall_time_range(
            datetime(2026, 6, 6, 0, 0, 0), datetime(2026, 6, 6, 23, 0, 0)
        )
        assert digest is None

    def test_read_missing_dir_is_graceful(self, tmp_path):
        store = LogStore(tmp_path / "does_not_exist")
        entries, complete = store.read_seq_range(0, 10)
        assert entries == []
        assert store.digest_seq_range(0, 10) is None


class TestBackfillChunks:
    def test_chunks_capped_by_max_and_cover_all(self, tmp_path):
        _write_shard(tmp_path / "interactions.jsonl", [
            _entry(i, i % 60, type_="message_in", participant_id="u", content="内容" * 20)
            for i in range(50)
        ])
        store = LogStore(tmp_path)
        chunks = store.backfill_chunks(max_chunks=5, target_chars=10)
        assert 0 < len(chunks) <= 6  # ~max_chunks（末块 +1）
        assert sum(len(c) for c in chunks) == 50  # 全覆盖
        flat = [e["seq"] for c in chunks for e in c]
        assert flat == sorted(flat)  # 时序

    def test_before_cutoff_excludes_recent(self, tmp_path):
        _write_shard(
            tmp_path / "interactions.jsonl",
            [_entry(i, i, content="x") for i in range(10)],
        )
        store = LogStore(tmp_path)
        chunks = store.backfill_chunks(
            before=datetime(2026, 6, 7, 9, 5, 0),
            max_chunks=64,
            target_chars=1,
        )
        seqs = [e["seq"] for c in chunks for e in c]
        assert seqs and all(s < 5 for s in seqs)  # 仅 ts < 09:05

    def test_empty_log_returns_no_chunks(self, tmp_path):
        assert LogStore(tmp_path / "empty").backfill_chunks() == []
        # 只有 system_prompt（非对话类）也应为空
        _write_shard(
            tmp_path / "interactions.jsonl",
            [_entry(0, 0, type_="system_prompt", content="x")],
        )
        assert LogStore(tmp_path).backfill_chunks() == []
