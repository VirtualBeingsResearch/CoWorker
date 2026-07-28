from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
from datetime import datetime
from pathlib import Path

import pytest

from coworker.agent.inbox_watcher import InboxWatcher
from coworker.agent.incoming_content import build_content_blocks
from coworker.core.autonomy import AutonomyLevel
from coworker.core.types import AttachmentData, IncomingEvent


def _event(participant_id: str = "alice", content: str = "hello") -> IncomingEvent:
    return IncomingEvent(participant_id=participant_id, content=content, timestamp=datetime.now())


class TestInboxWatcher:
    @pytest.mark.asyncio
    async def test_push_and_get_pending(self, tmp_path):
        watcher = InboxWatcher(str(tmp_path / "inbox"))
        await watcher.push(_event())
        events = await watcher.get_pending()
        assert len(events) == 1
        assert events[0].participant_id == "alice"

    @pytest.mark.asyncio
    async def test_get_pending_empty_queue(self, tmp_path):
        watcher = InboxWatcher(str(tmp_path / "inbox"))
        events = await watcher.get_pending()
        assert events == []

    @pytest.mark.asyncio
    async def test_push_multiple_events(self, tmp_path):
        watcher = InboxWatcher(str(tmp_path / "inbox"))
        for i in range(5):
            await watcher.push(_event(participant_id=f"user{i}", content=f"msg{i}"))
        events = await watcher.get_pending()
        assert len(events) == 5

    @pytest.mark.asyncio
    async def test_push_sets_message_event(self, tmp_path):
        watcher = InboxWatcher(str(tmp_path / "inbox"))
        assert not watcher.message_event.is_set()
        await watcher.push(_event())
        assert watcher.message_event.is_set()

    @pytest.mark.asyncio
    async def test_interceptors_run_in_registration_order(self, tmp_path):
        watcher = InboxWatcher(str(tmp_path / "inbox"))
        seen: list[str] = []
        watcher.set_interceptor(lambda event: seen.append("first") or False)
        watcher.add_interceptor(lambda event: seen.append("second") or False)

        await watcher.push(_event())

        assert seen == ["first", "second"]
        assert len(await watcher.get_pending()) == 1

    @pytest.mark.asyncio
    async def test_consuming_interceptor_stops_later_interceptors_and_main_inbox(self, tmp_path):
        watcher = InboxWatcher(str(tmp_path / "inbox"))
        seen: list[str] = []
        watcher.set_interceptor(lambda event: seen.append("first") or True)
        watcher.add_interceptor(lambda event: seen.append("second") or False)

        await watcher.push(_event())

        assert seen == ["first"]
        assert await watcher.get_pending() == []

    @pytest.mark.asyncio
    async def test_get_pending_clears_event_when_queue_empty(self, tmp_path):
        watcher = InboxWatcher(str(tmp_path / "inbox"))
        await watcher.push(_event())
        await watcher.get_pending()
        assert not watcher.message_event.is_set()

    @pytest.mark.asyncio
    async def test_get_pending_clears_event_after_multiple_items(self, tmp_path):
        watcher = InboxWatcher(str(tmp_path / "inbox"))
        await watcher.push(_event("alice"))
        await watcher.push(_event("bob"))
        await watcher.get_pending()
        assert not watcher.message_event.is_set()

    @pytest.mark.asyncio
    async def test_persistent_queue_peeks_until_explicit_ack(self, tmp_path):
        path = tmp_path / "pending_events.sqlite3"
        watcher = InboxWatcher(str(tmp_path / "inbox"), pending_path=path)
        event = _event()
        event.event_id = "stable-event"
        await watcher.push(event)

        [claimed] = await watcher.peek_pending(1)

        assert claimed.event_id == "stable-event"
        assert watcher.pending_count == 1
        restored = InboxWatcher(str(tmp_path / "inbox"), pending_path=path)
        assert restored.pending_count == 1

        await watcher.acknowledge(["stable-event"])

        assert watcher.pending_count == 0
        assert InboxWatcher(str(tmp_path / "inbox"), pending_path=path).pending_count == 0

    @pytest.mark.asyncio
    async def test_persistent_queue_deduplicates_stable_event_ids(self, tmp_path):
        watcher = InboxWatcher(
            str(tmp_path / "inbox"),
            pending_path=tmp_path / "pending_events.sqlite3",
        )
        first = _event(content="original")
        first.event_id = "same-event"
        duplicate = _event(content="duplicate")
        duplicate.event_id = "same-event"

        await watcher.push(first)
        await watcher.push(duplicate)

        assert watcher.pending_count == 1
        [event] = await watcher.peek_pending()
        assert event.content == "original"

    @pytest.mark.asyncio
    async def test_persistent_queue_reloads_attachment_data_from_saved_file(self, tmp_path):
        image_path = tmp_path / "saved.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
        pending_path = tmp_path / "pending_events.sqlite3"
        watcher = InboxWatcher(str(tmp_path / "inbox"), pending_path=pending_path)
        queued = IncomingEvent(
            participant_id="alice",
            content="image",
            attachments=[
                AttachmentData(
                    filename="saved.png",
                    media_type="image/png",
                    saved_path=str(image_path),
                    data="iVBORw0KGgo=",
                )
            ],
        )
        await watcher.push(queued)

        assert queued.attachments[0].data is None

        restored = InboxWatcher(str(tmp_path / "inbox"), pending_path=pending_path)
        [event] = await restored.peek_pending()

        assert event.attachments[0].data is None
        blocks = build_content_blocks([event])
        assert isinstance(blocks, list)
        assert any(block.get("type") == "image" for block in blocks)

    def test_persistent_queue_quarantines_invalid_rows(self, tmp_path):
        pending_path = tmp_path / "pending_events.sqlite3"
        watcher = InboxWatcher(str(tmp_path / "inbox"), pending_path=pending_path)
        assert watcher._pending_db is not None
        watcher._pending_db.execute(
            "INSERT INTO pending_events (event_id, payload) VALUES (?, ?)",
            ("broken", "[]"),
        )
        watcher._pending_db.commit()

        restored = InboxWatcher(str(tmp_path / "inbox"), pending_path=pending_path)

        assert restored.pending_count == 0
        assert restored._pending_db is not None
        [(event_id,)] = restored._pending_db.execute(
            "SELECT event_id FROM invalid_pending_events"
        ).fetchall()
        assert event_id == "broken"

    def test_persistent_queue_rejects_incompatible_schema(self, tmp_path):
        pending_path = tmp_path / "pending_events.sqlite3"
        connection = sqlite3.connect(pending_path)
        connection.execute("CREATE TABLE pending_events (unexpected TEXT)")
        connection.commit()
        connection.close()

        with pytest.raises(RuntimeError):
            InboxWatcher(str(tmp_path / "inbox"), pending_path=pending_path)

    @pytest.mark.asyncio
    async def test_message_event_wakes_up_waiter(self, tmp_path):
        watcher = InboxWatcher(str(tmp_path / "inbox"))

        async def push_after_delay():
            await asyncio.sleep(0.05)
            await watcher.push(_event())

        push_task = asyncio.create_task(push_after_delay())
        # Should complete quickly, not wait the full 5s
        await asyncio.wait_for(watcher.message_event.wait(), timeout=5.0)
        await push_task
        assert watcher.message_event.is_set()

    @pytest.mark.parametrize("stem,expected_sender", [
        ("20240101_120000_alice", "alice"),
        ("20240101_120000_bob_smith", "bob_smith"),
        ("nodatetime", "unknown"),
        ("ts_alice", "unknown"),
    ])
    def test_extract_sender(self, stem, expected_sender):
        assert InboxWatcher._extract_sender(stem) == expected_sender

    @pytest.mark.asyncio
    async def test_poll_reads_and_moves_file(self, tmp_path):
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        (inbox_dir / "processed").mkdir()

        msg_file = inbox_dir / "20240101_120000_alice.md"
        msg_file.write_text("hello from file", encoding="utf-8")

        watcher = InboxWatcher(str(inbox_dir))
        await watcher._poll()

        events = await watcher.get_pending()
        assert len(events) == 1
        assert events[0].participant_id == "alice"
        assert events[0].content == "hello from file"
        assert events[0].source == "file"

        assert not msg_file.exists()
        assert (inbox_dir / "processed" / "20240101_120000_alice.md").exists()

    @pytest.mark.asyncio
    async def test_poll_sets_message_event(self, tmp_path):
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        (inbox_dir / "processed").mkdir()
        (inbox_dir / "20240101_120000_alice.md").write_text("hi", encoding="utf-8")

        watcher = InboxWatcher(str(inbox_dir))
        assert not watcher.message_event.is_set()
        await watcher._poll()
        assert watcher.message_event.is_set()

    @pytest.mark.asyncio
    async def test_poll_deletes_empty_files(self, tmp_path):
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        (inbox_dir / "processed").mkdir()

        empty_file = inbox_dir / "20240101_120000_alice.md"
        empty_file.write_text("   \n  ", encoding="utf-8")

        watcher = InboxWatcher(str(inbox_dir))
        await watcher._poll()

        assert not empty_file.exists()
        events = await watcher.get_pending()
        assert len(events) == 0

    def test_poll_interval_property(self, tmp_path):
        watcher = InboxWatcher(str(tmp_path / "inbox"), poll_interval=5.0)
        assert watcher.poll_interval == 5.0
        watcher.poll_interval = 30.0
        assert watcher.poll_interval == 30.0

    @pytest.mark.asyncio
    async def test_poll_image_file_creates_attachment(self, tmp_path, monkeypatch):
        compact_id_with_separator = "abcde_fghijk"
        monkeypatch.setattr(
            "coworker.agent.inbox_watcher.new_compact_id",
            lambda: compact_id_with_separator,
        )
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        attachments_dir = tmp_path / "attachments"
        attachments_dir.mkdir()

        img_file = inbox_dir / "20240101_120000_alice.png"
        img_file.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG header

        watcher = InboxWatcher(str(inbox_dir))
        watcher._attachments = attachments_dir
        await watcher._poll()

        events = await watcher.get_pending()
        assert len(events) == 1
        event = events[0]
        assert event.participant_id == "alice"
        assert event.source == "file"
        assert len(event.attachments) == 1
        att = event.attachments[0]
        assert att.filename == "20240101_120000_alice.png"
        assert att.media_type == "image/png"
        assert att.data is None
        assert Path(att.saved_path).name == f"{compact_id_with_separator}_{att.filename}"
        blocks = build_content_blocks([event])
        assert isinstance(blocks, list)
        assert any(block.get("type") == "image" for block in blocks)
        if os.name != "nt":
            mode = stat.S_IMODE(Path(att.saved_path).stat().st_mode)
            assert mode == 0o600
        assert not img_file.exists()

    @pytest.mark.asyncio
    async def test_poll_unknown_extension_creates_attachment(self, tmp_path):
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        attachments_dir = tmp_path / "attachments"
        attachments_dir.mkdir()

        zip_file = inbox_dir / "20240101_120000_bob.zip"
        zip_file.write_bytes(b"PK\x03\x04")

        watcher = InboxWatcher(str(inbox_dir))
        watcher._attachments = attachments_dir
        await watcher._poll()

        events = await watcher.get_pending()
        assert len(events) == 1
        event = events[0]
        assert event.participant_id == "bob"
        assert len(event.attachments) == 1
        att = event.attachments[0]
        assert att.media_type == "application/octet-stream"
        assert att.data is None
        assert att.saved_path != ""
        assert not zip_file.exists()

    @pytest.mark.asyncio
    async def test_claimable_batch_skips_blocked_fifo_head(self, tmp_path):
        watcher = InboxWatcher(tmp_path / "inbox")
        await watcher.push(
            IncomingEvent(
                participant_id="alarm",
                content="blocked alarm",
                source="alarm",
                wake_level=AutonomyLevel.EVENT_DRIVEN,
            )
        )
        await watcher.push(
            IncomingEvent(
                participant_id="alice",
                content="direct",
                source="api",
                wake_level=AutonomyLevel.REACTIVE,
            )
        )

        selected = await watcher.peek_claimable(
            lambda event: event.wake_level in (None, AutonomyLevel.REACTIVE),
            limit=10,
        )

        assert [event.content for event in selected] == ["direct"]

    @pytest.mark.asyncio
    async def test_claimable_batch_reserves_wakeable_slot(self, tmp_path):
        watcher = InboxWatcher(tmp_path / "inbox")
        for index in range(3):
            await watcher.push(
                IncomingEvent(
                    participant_id="system",
                    content=f"notice-{index}",
                    source="system",
                    wake_level=None,
                )
            )
        await watcher.push(
            IncomingEvent(
                participant_id="alice",
                content="direct",
                source="api",
                wake_level=AutonomyLevel.REACTIVE,
            )
        )

        selected = await watcher.peek_claimable(lambda _: True, limit=2)

        assert [event.content for event in selected] == ["notice-0", "direct"]
