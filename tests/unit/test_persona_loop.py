"""Tests for persona card injection in the agent loop."""

from __future__ import annotations

from coworker.agent.loop import AgentLoop
from coworker.core.types import IncomingEvent
from coworker.memory.short_term import ShortTermMemory
from coworker.persona import PersonaCard, PersonAlias, PersonStore


def _make_loop(store: PersonStore, cards: PersonaCard) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop._person_store = store
    loop._persona_cards = cards
    loop._short_term = ShortTermMemory(max_tokens=10_000, tree_enabled=False)
    return loop


def _event(participant_id: str, content: str = "hi") -> IncomingEvent:
    return IncomingEvent(
        participant_id=participant_id,
        content=content,
        source="wecom",
    )


def _primary_sources(loop: AgentLoop) -> list[str]:
    return [getattr(m, "source", "") for m in loop._short_term.primary]


def _bound_person(store: PersonStore, *, notes: list[str] | None = None) -> object:
    person = store.create(display_name="张三", notes=notes or ["关系：好友"])
    store.bind_alias(person.person_id, PersonAlias("wecom:single:zs", channel="wecom"))
    return person


def test_inject_card_before_first_message(tmp_path) -> None:
    store = PersonStore(tmp_path / "persons.json")
    person = _bound_person(store, notes=["关系：好友"])
    cards = PersonaCard()

    loop = _make_loop(store, cards)
    resolved = loop._inject_persona_cards([_event("wecom:single:zs", "hello")])

    assert resolved == {"wecom:single:zs"}
    assert _primary_sources(loop) == [f"persona_card:{person.person_id}"]
    injected = loop._short_term.primary[0]
    assert person.person_id in injected.content
    assert "关系：好友" in injected.content


def test_card_injected_only_once_per_session(tmp_path) -> None:
    store = PersonStore(tmp_path / "persons.json")
    person = _bound_person(store)
    cards = PersonaCard()

    loop = _make_loop(store, cards)
    loop._inject_persona_cards([_event("wecom:single:zs")])
    loop._inject_persona_cards([_event("wecom:single:zs")])

    assert _primary_sources(loop) == [f"persona_card:{person.person_id}"]


def test_unbound_and_group_participants_get_no_card(tmp_path) -> None:
    store = PersonStore(tmp_path / "persons.json")
    _bound_person(store)
    cards = PersonaCard()

    loop = _make_loop(store, cards)
    resolved = loop._inject_persona_cards(
        [
            _event("wecom:group:room1"),
            _event("wecom:single:unknown"),
            _event("system"),
        ]
    )

    assert resolved == set()
    assert _primary_sources(loop) == []


def test_bound_person_with_bare_address_injects_address_card(tmp_path) -> None:
    """绑定地址本身就会渲染进画像框架（无需备注/称呼）。"""
    store = PersonStore(tmp_path / "persons.json")
    person = store.create()
    store.bind_alias(person.person_id, PersonAlias("wecom:single:zs"))
    cards = PersonaCard()

    loop = _make_loop(store, cards)
    resolved = loop._inject_persona_cards([_event("wecom:single:zs")])

    assert resolved == {"wecom:single:zs"}
    assert _primary_sources(loop) == [f"persona_card:{person.person_id}"]
    assert "wecom:single:zs" in loop._short_term.primary[0].content


def test_persona_disabled_no_injection(tmp_path) -> None:
    store = PersonStore(tmp_path / "persons.json")
    _bound_person(store)

    loop = _make_loop(None, None)
    resolved = loop._inject_persona_cards([_event("wecom:single:zs")])

    assert resolved == set()
    assert _primary_sources(loop) == []


def test_conversation_specific_alias(tmp_path) -> None:
    store = PersonStore(tmp_path / "persons.json")
    person = store.create(notes=["微信备注"])
    store.bind_alias(
        person.person_id,
        PersonAlias("weixin:bot1", conversation_id="session-9"),
    )
    cards = PersonaCard()

    loop = _make_loop(store, cards)
    resolved = loop._inject_persona_cards(
        [
            IncomingEvent(
                participant_id="weixin:bot1",
                conversation_id="session-9",
                content="hi",
                source="weixin",
            )
        ]
    )

    assert resolved == {"weixin:bot1"}
    assert _primary_sources(loop) == [f"persona_card:{person.person_id}"]
