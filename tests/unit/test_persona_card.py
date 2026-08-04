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
            PersonAlias("wecom:group:room1", channel="wecom"),  # 无备注也显示
        ],
        updated_at="2026-08-03T14:30:00",
    )
    rendered = cards.render(person)
    assert "工作日下午沟通更顺畅" in rendered
    assert "习惯用中文" in rendered
    assert "wecom:single:zs" in rendered
    assert "企业微信主号" in rendered
    assert "s9" in rendered
    assert "wecom:group:room1" in rendered  # 所有绑定地址都显示
    assert "2026-08-03" in rendered  # 更新时间（新鲜度信号）


def test_render_multiple_notes_per_address_on_own_lines() -> None:
    cards = PersonaCard()
    person = Person(
        person_id="p_abc",
        aliases=[
            PersonAlias(
                "wecom:single:zs",
                channel="wecom",
                notes=["企业微信主号", "周末常联系"],
            )
        ],
    )
    rendered = cards.render(person)
    lines = rendered.splitlines()
    address_index = next(i for i, line in enumerate(lines) if "wecom:single:zs" in line)
    # 地址独占一行，两条备注各自缩进成行
    assert lines[address_index].strip() == "- wecom:single:zs"
    assert lines[address_index + 1] == "  - 企业微信主号"
    assert lines[address_index + 2] == "  - 周末常联系"


def test_render_bare_addresses_only() -> None:
    cards = PersonaCard()
    person = Person(
        person_id="p_abc",
        aliases=[
            PersonAlias("wecom:single:zs", channel="wecom"),
            PersonAlias("weixin:bot1", conversation_id="s9", channel="weixin"),
        ],
    )
    rendered = cards.render(person)
    assert "wecom:single:zs" in rendered
    assert "weixin:bot1" in rendered
    assert "s9" in rendered


def test_render_person_without_name_uses_id() -> None:
    cards = PersonaCard()
    person = Person(person_id="p_abc", notes=["关系：好友"])
    rendered = cards.render(person)
    assert "p_abc" in rendered
    assert "关系：好友" in rendered
