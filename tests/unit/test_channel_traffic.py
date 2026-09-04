from __future__ import annotations

from pathlib import Path

from coworker.channels.traffic import ChannelTrafficStore


def test_channel_traffic_persists_metadata_and_filters(tmp_path: Path) -> None:
    path = tmp_path / "channel_traffic.jsonl"
    store = ChannelTrafficStore(path)
    store.record(
        direction="inbound",
        channel="wecom",
        participant_id="wecom:single:allowed",
        status="received",
        source="wecom",
    )
    store.record(
        direction="inbound",
        channel="wecom",
        participant_id="wecom:single:blocked",
        status="denied",
        source="wecom",
        reason="policy",
    )
    store.record(
        direction="outbound",
        channel="desktop",
        participant_id="coworker-desktop:desk:local:one",
        status="sent",
        source="agent",
    )

    restored = ChannelTrafficStore(path)
    entries = restored.recent(10)

    assert [entry["status"] for entry in entries] == ["sent", "denied", "received"]
    assert set(entries[0]) == {
        "ts",
        "direction",
        "channel",
        "participant_id",
        "status",
        "source",
        "reason",
    }
    assert "message" not in path.read_text(encoding="utf-8")
    assert [
        entry["participant_id"]
        for entry in restored.recent(10, direction="inbound", status="denied")
    ] == ["wecom:single:blocked"]
    assert [
        entry["participant_id"]
        for entry in restored.recent(10, channel="desktop")
    ] == ["coworker-desktop:desk:local:one"]


def test_channel_traffic_rotates_and_reads_newest_first(tmp_path: Path) -> None:
    path = tmp_path / "channel_traffic.jsonl"
    store = ChannelTrafficStore(path, max_bytes=260, backups=2)

    for index in range(6):
        store.record(
            direction="outbound",
            channel="stream",
            participant_id=f"participant-{index}",
            status="sent",
            source="agent",
        )

    assert path.with_name("channel_traffic.jsonl.1").is_file()
    assert ChannelTrafficStore(path, max_bytes=260, backups=2).recent(1)[0][
        "participant_id"
    ] == "participant-5"


def test_channel_traffic_skips_invalid_json_lines(tmp_path: Path) -> None:
    path = tmp_path / "channel_traffic.jsonl"
    path.write_text(
        '{"direction":"inbound","channel":"stream","participant_id":"ok",'
        '"status":"received","source":"rest","reason":"","ts":"now"}\n'
        "not-json\n",
        encoding="utf-8",
    )

    entries = ChannelTrafficStore(path).recent(10)

    assert [entry["participant_id"] for entry in entries] == ["ok"]


def test_channel_traffic_totals_track_incremental_records(tmp_path: Path) -> None:
    path = tmp_path / "channel_traffic.jsonl"
    store = ChannelTrafficStore(path)
    store.record(
        direction="inbound",
        channel="wecom",
        participant_id="wecom:single:allowed",
        status="received",
        source="wecom",
    )
    store.record(
        direction="inbound",
        channel="wecom",
        participant_id="wecom:single:blocked",
        status="denied",
        source="wecom",
    )
    store.record(
        direction="outbound",
        channel="desktop",
        participant_id="coworker-desktop:desk:local:one",
        status="sent",
        source="agent",
    )

    assert store.totals() == {
        "wecom": {"inbound": {"received": 1, "denied": 1}},
        "desktop": {"outbound": {"sent": 1}},
    }


def test_channel_traffic_totals_rebuild_from_retained_file(tmp_path: Path) -> None:
    path = tmp_path / "channel_traffic.jsonl"
    store = ChannelTrafficStore(path)
    for index in range(3):
        store.record(
            direction="outbound",
            channel="stream",
            participant_id=f"participant-{index}",
            status="sent",
            source="agent",
        )

    # 一个只为查询 recent() 新建的 store 也能在首次 totals() 时从保留文件重建。
    restored = ChannelTrafficStore(path)
    assert restored.recent(1)[0]["participant_id"] == "participant-2"
    assert restored.totals() == {"stream": {"outbound": {"sent": 3}}}


def test_channel_traffic_totals_cover_rotated_backups(tmp_path: Path) -> None:
    path = tmp_path / "channel_traffic.jsonl"
    store = ChannelTrafficStore(path, max_bytes=260, backups=2)

    for index in range(6):
        store.record(
            direction="outbound",
            channel="stream",
            participant_id=f"participant-{index}",
            status="sent",
            source="agent",
        )

    # 保留窗口为 .2 + .1 + active 三个文件（每个约一条）：重建计数覆盖全部备份。
    restored = ChannelTrafficStore(path, max_bytes=260, backups=2)
    assert restored.totals() == {"stream": {"outbound": {"sent": 3}}}


def test_channel_traffic_totals_document_window_semantics(tmp_path: Path) -> None:
    path = tmp_path / "channel_traffic.jsonl"
    store = ChannelTrafficStore(path, max_bytes=260, backups=0)
    store.record(
        direction="inbound",
        channel="telegram",
        participant_id="participant-0",
        status="received",
        source="telegram",
    )
    assert store.totals() == {"telegram": {"inbound": {"received": 1}}}

    for index in range(1, 20):
        store.record(
            direction="inbound",
            channel="telegram",
            participant_id=f"participant-{index}",
            status="received",
            source="telegram",
        )
    # 进程内计数单调递增（Prometheus 友好）：轮转不回退已初始化的计数。
    assert store.totals() == {"telegram": {"inbound": {"received": 20}}}
    # 「回落到保留窗口」只发生在重启后由新实例从磁盘重建时。
    assert ChannelTrafficStore(path, max_bytes=260, backups=0).totals() == {
        "telegram": {"inbound": {"received": 1}}
    }
