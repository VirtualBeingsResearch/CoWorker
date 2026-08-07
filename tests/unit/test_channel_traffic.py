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
