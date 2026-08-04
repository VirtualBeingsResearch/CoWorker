"""Tests for the standalone persona tool (bind/card/merge)."""

from __future__ import annotations

from coworker.persona import PersonaCard, PersonAlias, PersonStore
from coworker.tools.persona_tools import PersonaTool


def _tool(tmp_path) -> tuple[PersonaTool, PersonStore, PersonaCard]:
    store = PersonStore(tmp_path / "persons.json")
    cards = PersonaCard()
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


async def test_card_read_renders_framework_from_notes(tmp_path) -> None:
    tool, store, cards = _tool(tmp_path)
    person = store.create(display_name="张三", notes=["关系：好友"])
    read = await tool.execute(action="card", person_id=person.person_id)
    assert not read.is_error
    assert "张三" in read.content
    assert "关系：好友" in read.content
    assert cards.render(person) == read.content


async def test_card_empty_and_missing_person(tmp_path) -> None:
    tool, store, _ = _tool(tmp_path)
    person = store.create()
    empty = await tool.execute(action="card", person_id=person.person_id)
    assert not empty.is_error
    assert "暂无记录" in empty.content or "no recorded notes" in empty.content

    missing = await tool.execute(action="card", person_id="p_nope")
    assert missing.is_error


async def test_note_add_and_remove(tmp_path) -> None:
    tool, store, _ = _tool(tmp_path)
    person = store.create(display_name="张三")
    added = await tool.execute(
        action="note",
        person_id=person.person_id,
        note="习惯用中文",
    )
    assert not added.is_error
    assert person.notes == ["习惯用中文"]

    duplicate = await tool.execute(
        action="note",
        person_id=person.person_id,
        note="习惯用中文",
    )
    assert not duplicate.is_error
    assert person.notes == ["习惯用中文"]  # 去重

    removed = await tool.execute(
        action="note",
        person_id=person.person_id,
        note="习惯用中文",
        remove=True,
    )
    assert not removed.is_error
    assert person.notes == []

    missing = await tool.execute(action="note", person_id="p_nope", note="x")
    assert missing.is_error
    needs_content = await tool.execute(action="note", person_id=person.person_id)
    assert needs_content.is_error


async def test_unbind_removes_address(tmp_path) -> None:
    tool, store, _ = _tool(tmp_path)
    person = store.create(display_name="张三")
    store.bind_alias(person.person_id, PersonAlias("wecom:single:zs"))
    store.bind_alias(person.person_id, PersonAlias("weixin:bot1", conversation_id="s9"))

    result = await tool.execute(
        action="unbind",
        person_id=person.person_id,
        participant_id="wecom:single:zs",
    )
    assert not result.is_error
    assert store.find_by_participant("wecom:single:zs") is None
    assert store.find_by_participant("weixin:bot1", "s9") is person  # 其他地址保留

    # 会话专属地址需带 conversation_id 才能解绑
    stays = await tool.execute(
        action="unbind",
        person_id=person.person_id,
        participant_id="weixin:bot1",
    )
    assert stays.is_error
    assert store.find_by_participant("weixin:bot1", "s9") is person
    exact = await tool.execute(
        action="unbind",
        person_id=person.person_id,
        participant_id="weixin:bot1",
        conversation_id="s9",
    )
    assert not exact.is_error
    assert store.find_by_participant("weixin:bot1", "s9") is None


async def test_unbind_errors(tmp_path) -> None:
    tool, store, _ = _tool(tmp_path)
    person = store.create()
    needs_person = await tool.execute(action="unbind", participant_id="x")
    assert needs_person.is_error
    needs_address = await tool.execute(action="unbind", person_id=person.person_id)
    assert needs_address.is_error
    not_bound = await tool.execute(
        action="unbind",
        person_id=person.person_id,
        participant_id="wecom:single:unknown",
    )
    assert not_bound.is_error


async def test_delete_removes_person(tmp_path) -> None:
    tool, store, _ = _tool(tmp_path)
    person = store.create(display_name="张三", notes=["关系：好友"])
    result = await tool.execute(action="delete", person_id=person.person_id)
    assert not result.is_error
    assert store.get(person.person_id) is None

    missing = await tool.execute(action="delete", person_id=person.person_id)
    assert missing.is_error
    needs_person = await tool.execute(action="delete")
    assert needs_person.is_error


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
