from __future__ import annotations

from coworker.agent.bubble import Bubble, BubbleStore
from coworker.agent.bubble_router import BubbleMessageRouter
from coworker.core.autonomy import (
    AutonomyController,
    AutonomyLevel,
    AutonomyThresholds,
)
from coworker.core.types import AttachmentData, IncomingEvent, Message


def _bubble(store: BubbleStore, *, participant_id: str, conversation_id: str = "") -> Bubble:
    result = store.create("handle communication", [Message(role="user", content="start")], 5)
    assert isinstance(result, Bubble)
    result.participant_id = participant_id
    result.conversation_id = conversation_id
    return result


class TestBubbleMessageRouter:
    def test_routes_unambiguous_participant_message_to_bubble(self):
        store = BubbleStore()
        bubble = _bubble(store, participant_id="wecom:alice")
        router = BubbleMessageRouter(store)
        event = IncomingEvent(
            participant_id="wecom:alice",
            content="请继续处理",
            source="wecom",
        )

        assert router(event) is True
        assert bubble.inbox.get_nowait() is event

    def test_matching_conversation_wins_over_participant_only_binding(self):
        store = BubbleStore(max_concurrent=3)
        fallback = _bubble(store, participant_id="desk:alice")
        exact = _bubble(store, participant_id="desk:alice", conversation_id="thread-2")
        router = BubbleMessageRouter(store)
        event = IncomingEvent(
            participant_id="desk:alice",
            conversation_id="thread-2",
            content="thread follow-up",
            source="websocket",
        )

        assert router(event) is True
        assert exact.inbox.get_nowait() is event
        assert fallback.inbox.empty()

    def test_ambiguous_binding_falls_back_to_main_inbox(self):
        store = BubbleStore(max_concurrent=3)
        _bubble(store, participant_id="alice")
        _bubble(store, participant_id="alice")
        router = BubbleMessageRouter(store)

        assert router(IncomingEvent(participant_id="alice", content="hello")) is False

    def test_internal_event_is_never_routed_to_bubble(self):
        store = BubbleStore()
        bubble = _bubble(store, participant_id="system")
        router = BubbleMessageRouter(store)

        assert router(IncomingEvent(participant_id="system", content="notice", source="system")) is False
        assert bubble.inbox.empty()

    def test_policy_paused_bubble_leaves_direct_message_for_durable_main_inbox(self):
        store = BubbleStore()
        bubble = _bubble(store, participant_id="wecom:alice")
        bubble.origin_trigger = AutonomyLevel.AUTONOMOUS
        bubble.brain = type(
            "PolicyBrain",
            (),
            {
                "autonomy": AutonomyController(
                    AutonomyLevel.REACTIVE,
                    AutonomyThresholds(),
                )
            },
        )()
        router = BubbleMessageRouter(store)
        event = IncomingEvent(
            participant_id="wecom:alice",
            content="请先回复我",
            source="wecom",
            wake_level=AutonomyLevel.REACTIVE,
        )

        assert router(event) is False
        assert bubble.inbox.empty()

    def test_preserves_attachment_and_conversation_metadata(self):
        store = BubbleStore()
        bubble = _bubble(store, participant_id="alice", conversation_id="conv-1")
        router = BubbleMessageRouter(store)
        event = IncomingEvent(
            participant_id="alice",
            conversation_id="conv-1",
            content="see file",
            source="rest",
            attachments=[
                AttachmentData(
                    filename="note.txt",
                    media_type="text/plain",
                    saved_path="data/attachments/note.txt",
                )
            ],
        )

        assert router(event) is True
        queued = bubble.inbox.get_nowait()
        assert queued.conversation_id == "conv-1"
        assert queued.attachments[0].filename == "note.txt"
