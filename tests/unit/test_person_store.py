"""Tests for the persona PersonStore and Person/PersonAlias model."""

from __future__ import annotations

from coworker.persona import PersonAlias, PersonStore


def test_create_persists_and_round_trips(tmp_path) -> None:
    store = PersonStore(tmp_path / "persons.json")
    person = store.create(display_name="张三", aliases=[PersonAlias("wecom:single:zs")])
    assert person.person_id.startswith("p")
    assert store.get(person.person_id) is person

    reloaded = PersonStore(tmp_path / "persons.json")
    loaded = reloaded.get(person.person_id)
    assert loaded is not None
    assert loaded.display_name == "张三"
    assert loaded.aliases[0].participant_id == "wecom:single:zs"


def test_find_by_participant_generic_and_conversation(tmp_path) -> None:
    store = PersonStore(tmp_path / "persons.json")
    alice = store.create(display_name="Alice", aliases=[PersonAlias("weixin:bot1")])
    bob = store.create(
        display_name="Bob",
        aliases=[PersonAlias("weixin:bot2", conversation_id="session-9")],
    )

    # Generic alias matches any conversation.
    assert store.find_by_participant("weixin:bot1") is alice
    assert store.find_by_participant("weixin:bot1", conversation_id="session-x") is alice
    # Conversation-specific alias only matches that conversation.
    assert store.find_by_participant("weixin:bot2", conversation_id="session-9") is bob
    assert store.find_by_participant("weixin:bot2") is None
    assert store.find_by_participant("weixin:bot2", conversation_id="other") is None
    # Unbound / empty.
    assert store.find_by_participant("wecom:single:unknown") is None
    assert store.find_by_participant("") is None


def test_update_display_name(tmp_path) -> None:
    store = PersonStore(tmp_path / "persons.json")
    person = store.create(display_name="旧名")
    updated = store.update(person.person_id, display_name="新名")
    assert updated is not None and updated.display_name == "新名"
    assert store.update("p_missing", display_name="x") is None
    assert PersonStore(tmp_path / "persons.json").get(person.person_id).display_name == "新名"


def test_bind_alias_idempotent(tmp_path) -> None:
    store = PersonStore(tmp_path / "persons.json")
    person = store.create()
    store.bind_alias(person.person_id, PersonAlias("wecom:single:zs", channel="wecom"))
    store.bind_alias(person.person_id, PersonAlias("wecom:single:zs", channel="wecom"))
    assert len(person.aliases) == 1
    # Different conversation is a distinct alias.
    store.bind_alias(person.person_id, PersonAlias("weixin:bot", conversation_id="s1"))
    assert len(person.aliases) == 2
    assert store.bind_alias("p_missing", PersonAlias("x")) is None


def test_delete(tmp_path) -> None:
    store = PersonStore(tmp_path / "persons.json")
    person = store.create()
    assert store.delete(person.person_id) is True
    assert store.get(person.person_id) is None
    assert store.delete(person.person_id) is False


def test_merge_unions_aliases_and_drops_other(tmp_path) -> None:
    store = PersonStore(tmp_path / "persons.json")
    keep = store.create(display_name="A", aliases=[PersonAlias("wecom:single:a")])
    drop = store.create(aliases=[PersonAlias("weixin:bot1"), PersonAlias("weixin:bot2")])
    store.merge(keep.person_id, drop.person_id)

    merged = store.get(keep.person_id)
    assert merged is not None
    assert {a.participant_id for a in merged.aliases} == {
        "wecom:single:a",
        "weixin:bot1",
        "weixin:bot2",
    }
    assert store.get(drop.person_id) is None
    # Invalid merges.
    assert store.merge(keep.person_id, "p_missing") is None
    assert store.merge(keep.person_id, keep.person_id) is None


def test_merge_keeps_display_name_when_keep_empty(tmp_path) -> None:
    store = PersonStore(tmp_path / "persons.json")
    keep = store.create(aliases=[PersonAlias("wecom:single:a")])
    drop = store.create(display_name="老王", aliases=[PersonAlias("weixin:bot1")])
    store.merge(keep.person_id, drop.person_id)
    assert store.get(keep.person_id).display_name == "老王"


def test_load_ignores_corrupt_file(tmp_path) -> None:
    path = tmp_path / "persons.json"
    path.write_text("{not json", encoding="utf-8")
    store = PersonStore(path)
    assert store.all_persons() == []
    # A subsequent save overwrites the corrupt file.
    person = store.create()
    assert path.is_file()
    assert PersonStore(path).get(person.person_id) is not None
