from __future__ import annotations

from coworker.channels.stream.desktop import DesktopRegistry
from coworker.memory.short_term import ShortTermMemory


def _snapshot(actor: str = "claude") -> dict:
    return {
        "protocol_version": 1,
        "message_id": "019f-test",
        "created_at": "2026-07-12T00:00:00Z",
        "type": "desktop.actor.snapshot",
        "payload": {
            "desktop_id": "desk-a",
            "display_name": "Alice Desktop",
            "actor_id": actor,
            "available": True,
            "required_skill": "coworker-desktop",
            "projects": [
                {
                    "project_id": "d:/projects/coworker",
                    "name": "coworker",
                    "path": r"D:\Projects\coworker",
                    "matched_conversation_count": 2,
                    "shown_conversation_count": 1,
                    "complete": False,
                    "truncated": True,
                    "recent_conversations": [
                        {
                            "conversation_id": "thread-1",
                            "title": "修复项目快照标题过长测试内容",
                            "writable": True,
                            "updated_at": "2026-07-14T00:00:00Z",
                            "mode": "default",
                        }
                    ],
                },
                {
                    "project_id": "no-project",
                    "name": "对话",
                    "scope": "conversation",
                    "recent_conversations": [
                        {"conversation_id": "chat-1", "title": "普通对话"}
                    ],
                },
            ],
        },
    }


def test_snapshot_creates_actor_scoped_pin(tmp_path):
    memory = ShortTermMemory()
    registry = DesktopRegistry(memory, tmp_path)
    participant = "coworker-desktop:desk-a:claude:cw:123"
    registry.update_connections({participant})

    consumed = registry.ingest_snapshot(_snapshot()["payload"], participant)

    assert consumed is True
    assert registry.actors["desk-a:claude"].participant_id == participant
    pin = next(item for item in memory.list_pinned() if item.pin_id == "coworker_desktop_registry")
    assert "coworker-desktop` Skill" in pin.content
    assert participant in pin.content
    assert "- Alice Desktop" in pin.content
    assert "claude → participant_id=" in pin.content
    assert "coworker · project_id=d:/projects/coworker" in pin.content
    assert r"D:\Projects\coworker" not in pin.content
    assert "近期仅展示 1/2" in pin.content
    assert "complete=false" not in pin.content
    assert "status: connected" not in pin.content
    assert "thread-1 · 修复项目快照标题过长测试内容（2026-07-14）" in pin.content
    assert "mode=default" not in pin.content
    assert "- 对话" in pin.content
    assert "chat-1 · 普通对话" in pin.content


def test_disconnected_actor_is_removed_from_registry(tmp_path):
    memory = ShortTermMemory()
    registry = DesktopRegistry(memory, tmp_path)
    participant = "coworker-desktop:desk-a:local:cw:123"
    registry.update_connections({participant})
    registry.ingest_snapshot(_snapshot("local")["payload"], participant)

    registry.update_connections(set())

    assert registry.actors == {}
    assert all(item.pin_id != "coworker_desktop_registry" for item in memory.list_pinned())


def test_flat_legacy_conversations_are_not_rendered(tmp_path):
    memory = ShortTermMemory()
    registry = DesktopRegistry(memory, tmp_path)
    event = _snapshot()
    event["payload"].pop("projects")
    event["payload"]["conversations"] = [
        {"conversation_id": "legacy-thread", "title": "Legacy"}
    ]

    assert registry.ingest_snapshot(event["payload"], "desktop")
    pin = next(
        item for item in memory.list_pinned() if item.pin_id == "coworker_desktop_registry"
    )
    assert "无近期会话" in pin.content
    assert "legacy-thread" not in pin.content


def test_pin_groups_actors_and_limits_recent_conversations(tmp_path):
    memory = ShortTermMemory()
    registry = DesktopRegistry(memory, tmp_path)
    claude = _snapshot("claude")["payload"]
    claude["projects"][0]["matched_conversation_count"] = 6
    claude["projects"][0]["shown_conversation_count"] = 6
    claude["projects"][0]["complete"] = True
    claude["projects"][0]["truncated"] = False
    claude["projects"][0]["recent_conversations"] = [
        {
            "conversation_id": f"claude-thread-{index}",
            "title": f"会话 {index}",
            "updated_at": f"2026-07-{index:02d}T00:00:00Z",
            "mode": "plan" if index == 6 else "default",
        }
        for index in range(1, 7)
    ]
    local = _snapshot("local")["payload"]

    registry.ingest_snapshot(
        claude,
        "coworker-desktop:desk-a:claude:cw:123",
    )
    registry.ingest_snapshot(
        local,
        "coworker-desktop:desk-a:local:cw:456",
    )

    pin = next(
        item for item in memory.list_pinned() if item.pin_id == "coworker_desktop_registry"
    )
    assert pin.content.count("- Alice Desktop") == 1
    assert "claude → participant_id=" in pin.content
    assert "local → participant_id=" in pin.content
    assert "近期仅展示 4/6" in pin.content
    assert "claude-thread-6 · 会话 6（plan, 2026-07-06）" in pin.content
    assert "claude-thread-3 · 会话 3（2026-07-03）" in pin.content
    assert pin.content.index("claude-thread-6") < pin.content.index("claude-thread-3")
    assert "claude-thread-2" not in pin.content
    assert "claude-thread-1" not in pin.content
