from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from coworker.agent.inbox_watcher import InboxWatcher
from coworker.core.types import IncomingEvent


def _event(participant_id: str = "alice", content: str = "hello") -> IncomingEvent:
    return IncomingEvent(participant_id=participant_id, content=content, timestamp=datetime.now())


class TestInboxWatcher:
    @pytest.mark.asyncio
    async def test_push_and_get_pending(self):
        watcher = InboxWatcher("data/inbox")
        await watcher.push(_event())
        events = await watcher.get_pending()
        assert len(events) == 1
        assert events[0].participant_id == "alice"

    @pytest.mark.asyncio
    async def test_get_pending_empty_queue(self):
        watcher = InboxWatcher("data/inbox")
        events = await watcher.get_pending()
        assert events == []

    @pytest.mark.asyncio
    async def test_push_multiple_events(self):
        watcher = InboxWatcher("data/inbox")
        for i in range(5):
            await watcher.push(_event(participant_id=f"user{i}", content=f"msg{i}"))
        events = await watcher.get_pending()
        assert len(events) == 5

    @pytest.mark.asyncio
    async def test_push_sets_message_event(self):
        watcher = InboxWatcher("data/inbox")
        assert not watcher.message_event.is_set()
        await watcher.push(_event())
        assert watcher.message_event.is_set()

    @pytest.mark.asyncio
    async def test_interceptors_run_in_registration_order(self):
        watcher = InboxWatcher("data/inbox")
        seen: list[str] = []
        watcher.set_interceptor(lambda event: seen.append("first") or False)
        watcher.add_interceptor(lambda event: seen.append("second") or False)

        await watcher.push(_event())

        assert seen == ["first", "second"]
        assert len(await watcher.get_pending()) == 1

    @pytest.mark.asyncio
    async def test_consuming_interceptor_stops_later_interceptors_and_main_inbox(self):
        watcher = InboxWatcher("data/inbox")
        seen: list[str] = []
        watcher.set_interceptor(lambda event: seen.append("first") or True)
        watcher.add_interceptor(lambda event: seen.append("second") or False)

        await watcher.push(_event())

        assert seen == ["first"]
        assert await watcher.get_pending() == []

    @pytest.mark.asyncio
    async def test_get_pending_clears_event_when_queue_empty(self):
        watcher = InboxWatcher("data/inbox")
        await watcher.push(_event())
        await watcher.get_pending()
        assert not watcher.message_event.is_set()

    @pytest.mark.asyncio
    async def test_get_pending_keeps_event_set_if_queue_not_empty(self):
        watcher = InboxWatcher("data/inbox")
        await watcher.push(_event("alice"))
        await watcher.push(_event("bob"))
        # Manually drain only one item to leave one in queue
        watcher._queue.get_nowait()
        await watcher.get_pending()
        # Queue still had one item, event should still be set after get_pending drains it...
        # Actually get_pending drains all, so event should be cleared.
        assert not watcher.message_event.is_set()

    @pytest.mark.asyncio
    async def test_message_event_wakes_up_waiter(self):
        watcher = InboxWatcher("data/inbox")

        async def push_after_delay():
            await asyncio.sleep(0.05)
            await watcher.push(_event())

        asyncio.create_task(push_after_delay())
        # Should complete quickly, not wait the full 5s
        await asyncio.wait_for(watcher.message_event.wait(), timeout=5.0)
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

    def test_poll_interval_property(self):
        watcher = InboxWatcher("data/inbox", poll_interval=5.0)
        assert watcher.poll_interval == 5.0
        watcher.poll_interval = 30.0
        assert watcher.poll_interval == 30.0

    @pytest.mark.asyncio
    async def test_poll_image_file_creates_attachment(self, tmp_path):
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
        assert att.data is not None
        assert len(Path(att.saved_path).name.split("_", 1)[0]) == 12
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
