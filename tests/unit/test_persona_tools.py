"""Tests for the standalone persona tool (bind/card/merge)."""

from __future__ import annotations

from coworker.persona import PersonaCard, PersonAlias, PersonStore
from coworker.persona.tools import PersonaTool


def _tool(tmp_path) -> tuple[PersonaTool, PersonStore, PersonaCard]:
    store = PersonStore(tmp_path / "persons.json")
    cards = PersonaCard(tmp_path / "cards")
    return PersonaTool(store, cards), store, cards


async def test_bind_creates_new_person(tmp_path) -> None:
    tool, store, _ = _tool(tmp_path)
    result = await tool.execute(
        action="bind",
        participant_id="wecom:single:zs",
        name="张三",
    )
    assert not result.is_error
    person = store.all_persons()[0]
    assert person.display_name == "张三"
    assert person.aliases[0].participant_id == "wecom:single:zs"


async def test_bind_appends_notes_to_same_address(tmp_path) -> None:
    tool, store, _ = _tool(tmp_path)
    first = await tool.execute(
        action="bind",
        participant_id="wecom:single:zs",
        name="张三",
        note="工作伙伴",
    )
    assert not first.is_error
    second = await tool.execute(
        action="bind",
        participant_id="wecom:single:zs",
        name="张三",
        note="周末常联系",
    )
    assert not second.is_error
    person = store.all_persons()[0]
    assert person.aliases[0].notes == ["工作伙伴", "周末常联系"]
    assert len(person.aliases) == 1  # 同一地址不重复建别名


async def test_bind_to_existing_person_by_id(tmp_path) -> None:
    tool, store, _ = _tool(tmp_path)
    person = store.create(display_name="张三")
    result = await tool.execute(
        action="bind",
        participant_id="weixin:bot1",
        conversation_id="session-9",
        person_id=person.person_id,
    )
    assert not result.is_error
    assert store.find_by_participant("weixin:bot1", "session-9") is person
    assert len(store.all_persons()) == 1  # no duplicate person created


async def test_bind_by_name_matches_case_insensitive(tmp_path) -> None:
    tool, store, _ = _tool(tmp_path)
    person = store.create(display_name="张三")
    result = await tool.execute(
        action="bind",
        participant_id="weixin:bot1",
        name="张三",
    )
    assert not result.is_error
    assert store.find_by_participant("weixin:bot1") is person
    assert len(store.all_persons()) == 1


async def test_bind_errors(tmp_path) -> None:
    tool, _, _ = _tool(tmp_path)
    missing = await tool.execute(action="bind")
    assert missing.is_error
    bad_id = await tool.execute(
        action="bind",
        participant_id="wecom:single:zs",
        person_id="p_nope",
    )
    assert bad_id.is_error


async def test_card_write_then_read(tmp_path) -> None:
    tool, store, cards = _tool(tmp_path)
    person = store.create(display_name="张三")
    updated = await tool.execute(
        action="card",
        person_id=person.person_id,
        content="# 张三\n- 关系：好友",
    )
    assert not updated.is_error
    assert cards.load(person.person_id).startswith("# 张三")

    read = await tool.execute(action="card", person_id=person.person_id)
    assert not read.is_error
    assert "关系：好友" in read.content


async def test_card_empty_and_missing_person(tmp_path) -> None:
    tool, store, _ = _tool(tmp_path)
    person = store.create()
    empty = await tool.execute(action="card", person_id=person.person_id)
    assert not empty.is_error
    assert "暂无画像" in empty.content or "no persona card" in empty.content

    missing = await tool.execute(action="card", person_id="p_nope")
    assert missing.is_error


async def test_merge_unions_people(tmp_path) -> None:
    tool, store, _ = _tool(tmp_path)
    keep = store.create(display_name="张三")
    drop = store.create(aliases=[PersonAlias("weixin:bot1")])
    result = await tool.execute(
        action="merge",
        keep_person_id=keep.person_id,
        drop_person_id=drop.person_id,
    )
    assert not result.is_error
    assert store.get(drop.person_id) is None
    assert store.find_by_participant("weixin:bot1") is keep


async def test_merge_errors(tmp_path) -> None:
    tool, store, _ = _tool(tmp_path)
    keep = store.create()
    missing = await tool.execute(
        action="merge",
        keep_person_id=keep.person_id,
        drop_person_id="p_nope",
    )
    assert missing.is_error
    missing_ids = await tool.execute(action="merge")
    assert missing_ids.is_error


async def test_unknown_action(tmp_path) -> None:
    tool, _, _ = _tool(tmp_path)
    result = await tool.execute(action="nope")
    assert result.is_error
