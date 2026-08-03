"""Tests for the persona card framework renderer."""

from __future__ import annotations

from coworker.persona import Person, PersonaCard, PersonAlias


def test_render_empty_person_returns_empty() -> None:
    cards = PersonaCard()
    assert cards.render(Person(person_id="p_abc")) == ""


def test_render_header_with_display_name() -> None:
    cards = PersonaCard()
    rendered = cards.render(Person(person_id="p_abc", display_name="张三"))
    assert "张三" in rendered
    assert "p_abc" in rendered


def test_render_person_notes_and_alias_notes() -> None:
    cards = PersonaCard()
    person = Person(
        person_id="p_abc",
        display_name="张三",
        notes=["工作日下午沟通更顺畅", "习惯用中文"],
        aliases=[
            PersonAlias("wecom:single:zs", channel="wecom", notes=["企业微信主号"]),
            PersonAlias("weixin:bot1", conversation_id="s9", channel="weixin", notes=["微信"]),
            PersonAlias("wecom:group:room1", channel="wecom"),  # 无备注不出现
        ],
    )
    rendered = cards.render(person)
    assert "工作日下午沟通更顺畅" in rendered
    assert "习惯用中文" in rendered
    assert "wecom:single:zs" in rendered
    assert "企业微信主号" in rendered
    assert "s9" in rendered
    assert "wecom:group:room1" not in rendered  # 无备注的地址不进框架


def test_render_person_without_name_uses_id() -> None:
    cards = PersonaCard()
    person = Person(person_id="p_abc", notes=["关系：好友"])
    rendered = cards.render(person)
    assert "p_abc" in rendered
    assert "关系：好友" in rendered
