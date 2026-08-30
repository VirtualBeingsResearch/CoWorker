"""Tests for the optional persona sub-mechanism (store, card, loop injection, tool)."""


from coworker.agent.loop import AgentLoop
from coworker.channels.modelapi.tokens import (
    IssuedToken,
    ModelApiTokenError,
    TokenDetail,
    TokenSummary,
)
from coworker.core.types import IncomingEvent
from coworker.memory.short_term import ShortTermMemory
from coworker.persona import Person, PersonaCard, PersonaContext, PersonAlias, PersonStore
from coworker.tools.persona_tools import PersonaTool


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


def test_alias_notes_round_trip_and_legacy_note_compat(tmp_path) -> None:
    path = tmp_path / "persons.json"
    store = PersonStore(path)
    person = store.create(aliases=[PersonAlias("wecom:single:zs", notes=["工作伙伴"])])
    assert person.aliases[0].notes == ["工作伙伴"]

    reloaded = PersonStore(path).get(person.person_id)
    assert reloaded.aliases[0].notes == ["工作伙伴"]

    # 旧格式：单条字符串 note 读取为单元素列表。
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    data["persons"][0]["aliases"][0]["note"] = "旧备注"
    del data["persons"][0]["aliases"][0]["notes"]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    legacy = PersonStore(path).get(person.person_id)
    assert legacy.aliases[0].notes == ["旧备注"]


def test_bind_alias_merges_notes(tmp_path) -> None:
    store = PersonStore(tmp_path / "persons.json")
    person = store.create()
    store.bind_alias(person.person_id, PersonAlias("wecom:single:zs", notes=["工作伙伴"]))
    store.bind_alias(person.person_id, PersonAlias("wecom:single:zs", notes=["周末常联系"]))
    store.bind_alias(person.person_id, PersonAlias("wecom:single:zs", notes=["工作伙伴"]))
    assert person.aliases[0].notes == ["工作伙伴", "周末常联系"]


def test_load_ignores_corrupt_file(tmp_path) -> None:
    path = tmp_path / "persons.json"
    path.write_text("{not json", encoding="utf-8")
    store = PersonStore(path)
    assert store.all_persons() == []
    # A subsequent save overwrites the corrupt file.
    person = store.create()
    assert path.is_file()
    assert PersonStore(path).get(person.person_id) is not None



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



def _make_loop(context: PersonaContext | None) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop._persona = context
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

    loop = _make_loop(PersonaContext(store=store, cards=cards))
    loop._inject_persona_cards([_event("wecom:single:zs", "hello")])
    assert _primary_sources(loop) == ["persona"]
    injected = loop._short_term.primary[0]
    assert injected.source == "persona"
    assert injected.person_id == person.person_id
    assert person.person_id in injected.content
    assert "关系：好友" in injected.content


def test_persona_card_marker_survives_snapshot_round_trip(tmp_path) -> None:
    store = PersonStore(tmp_path / "persons.json")
    person = _bound_person(store)
    cards = PersonaCard()

    loop = _make_loop(PersonaContext(store=store, cards=cards))
    loop._inject_persona_cards([_event("wecom:single:zs")])

    restored = ShortTermMemory.parse_primary(loop._short_term.serialize())
    assert restored[0].source == "persona"
    assert restored[0].person_id == person.person_id


def test_card_injected_only_once_per_session(tmp_path) -> None:
    store = PersonStore(tmp_path / "persons.json")
    _bound_person(store)
    cards = PersonaCard()

    loop = _make_loop(PersonaContext(store=store, cards=cards))
    loop._inject_persona_cards([_event("wecom:single:zs")])
    loop._inject_persona_cards([_event("wecom:single:zs")])

    assert _primary_sources(loop) == ["persona"]


def test_unbound_and_group_participants_get_no_card(tmp_path) -> None:
    store = PersonStore(tmp_path / "persons.json")
    _bound_person(store)
    cards = PersonaCard()

    loop = _make_loop(PersonaContext(store=store, cards=cards))
    loop._inject_persona_cards(
        [
            _event("wecom:group:room1"),
            _event("wecom:single:unknown"),
            _event("system"),
        ]
    )
    assert _primary_sources(loop) == []


def test_bound_person_with_bare_address_injects_address_card(tmp_path) -> None:
    """绑定地址本身就会渲染进画像框架（无需备注/称呼）。"""
    store = PersonStore(tmp_path / "persons.json")
    person = store.create()
    store.bind_alias(person.person_id, PersonAlias("wecom:single:zs"))
    cards = PersonaCard()

    loop = _make_loop(PersonaContext(store=store, cards=cards))
    loop._inject_persona_cards([_event("wecom:single:zs")])
    assert _primary_sources(loop) == ["persona"]
    assert "wecom:single:zs" in loop._short_term.primary[0].content


def test_persona_disabled_no_injection(tmp_path) -> None:
    store = PersonStore(tmp_path / "persons.json")
    _bound_person(store)

    loop = _make_loop(None)
    loop._inject_persona_cards([_event("wecom:single:zs")])
    assert _primary_sources(loop) == []


def test_conversation_specific_alias(tmp_path) -> None:
    store = PersonStore(tmp_path / "persons.json")
    person = store.create(notes=["微信备注"])
    store.bind_alias(
        person.person_id,
        PersonAlias("weixin:bot1", conversation_id="session-9"),
    )
    cards = PersonaCard()

    loop = _make_loop(PersonaContext(store=store, cards=cards))
    loop._inject_persona_cards(
        [
            IncomingEvent(
                participant_id="weixin:bot1",
                conversation_id="session-9",
                content="hi",
                source="weixin",
            )
        ]
    )
    assert _primary_sources(loop) == ["persona"]




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


async def test_multiline_note_rejected_with_error(tmp_path) -> None:
    tool, store, _ = _tool(tmp_path)
    person = store.create(display_name="张三")
    result = await tool.execute(
        action="note",
        person_id=person.person_id,
        notes=["工作日下午\n沟通更顺畅"],
    )
    assert result.is_error
    assert "单行" in result.content or "single line" in result.content  # 错误信息说明单行要求
    assert person.notes == []  # 未写入


async def test_bind_multiline_note_rejected(tmp_path) -> None:
    tool, store, _ = _tool(tmp_path)
    result = await tool.execute(
        action="bind",
        participant_id="wecom:single:zs",
        name="张三",
        note="企业微信主号\n周末常联系",
    )
    assert result.is_error
    assert "单行" in result.content or "single line" in result.content
    assert store.all_persons() == []  # 未创建人物/别名


async def test_note_adds_multiple_notes_in_one_call(tmp_path) -> None:
    tool, store, _ = _tool(tmp_path)
    person = store.create(display_name="张三")
    result = await tool.execute(
        action="note",
        person_id=person.person_id,
        notes=["习惯用中文", "工作日下午沟通更顺畅"],
    )
    assert not result.is_error
    assert person.notes == ["习惯用中文", "工作日下午沟通更顺畅"]


async def test_note_multiple_with_multiline_rejected_atomically(tmp_path) -> None:
    tool, store, _ = _tool(tmp_path)
    person = store.create(display_name="张三")
    result = await tool.execute(
        action="note",
        person_id=person.person_id,
        notes=["习惯用中文", "工作日下午\n沟通更顺畅"],
    )
    assert result.is_error
    assert "单行" in result.content or "single line" in result.content
    assert person.notes == []  # 全部不写入，无部分成功


async def test_note_accepts_notes_as_single_string(tmp_path) -> None:
    tool, store, _ = _tool(tmp_path)
    person = store.create(display_name="张三")
    result = await tool.execute(
        action="note",
        person_id=person.person_id,
        notes="习惯用中文",
    )
    assert not result.is_error
    assert person.notes == ["习惯用中文"]


async def test_note_removes_multiple_notes_in_one_call(tmp_path) -> None:
    tool, store, _ = _tool(tmp_path)
    person = store.create(display_name="张三", notes=["习惯用中文", "工作日下午沟通更顺畅"])
    result = await tool.execute(
        action="note",
        person_id=person.person_id,
        notes=["习惯用中文", "工作日下午沟通更顺畅"],
        remove=True,
    )
    assert not result.is_error
    assert person.notes == []


async def test_bind_trailing_newline_is_fine(tmp_path) -> None:
    """仅末尾换行属于空白，strip 后仍是单行，不应报错。"""
    tool, store, _ = _tool(tmp_path)
    result = await tool.execute(
        action="bind",
        participant_id="wecom:single:zs",
        name="张三",
        note="企业微信主号\n",
    )
    assert not result.is_error
    person = store.all_persons()[0]
    assert person.aliases[0].notes == ["企业微信主号"]


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
        notes=["习惯用中文"],
    )
    assert not added.is_error
    assert person.notes == ["习惯用中文"]

    duplicate = await tool.execute(
        action="note",
        person_id=person.person_id,
        notes=["习惯用中文"],
    )
    assert not duplicate.is_error
    assert person.notes == ["习惯用中文"]  # 去重

    removed = await tool.execute(
        action="note",
        person_id=person.person_id,
        notes=["习惯用中文"],
        remove=True,
    )
    assert not removed.is_error
    assert person.notes == []

    missing = await tool.execute(action="note", person_id="p_nope", notes=["x"])
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


class _StubTokenService:
    """Duck-typed ModelApiTokenService double that records calls."""

    def __init__(self, *, issued=None, summaries=None, detail=None, error=None) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._issued = issued
        self._summaries = summaries or []
        self._detail = detail
        self._error = error

    async def issue(self, person_id, *, note="", key="", origin="admin"):
        self.calls.append(("issue", person_id, {"note": note, "key": key, "origin": origin}))
        if self._error is not None:
            raise self._error
        return self._issued

    async def revoke(self, person_id, key, *, origin="admin"):
        self.calls.append(("revoke", person_id, {"key": key, "origin": origin}))
        if self._error is not None:
            raise self._error
        return None

    def list_for_person(self, person_id):
        self.calls.append(("list", person_id, {}))
        if self._error is not None:
            raise self._error
        return self._summaries

    def read_plaintext(self, person_id, key, *, origin="admin"):
        self.calls.append(("read", person_id, {"key": key, "origin": origin}))
        if self._error is not None:
            raise self._error
        return self._detail


def _token_tool(tmp_path, service) -> tuple[PersonaTool, PersonStore]:
    store = PersonStore(tmp_path / "persons.json")
    return PersonaTool(store, PersonaCard(), tokens=service), store


async def test_issue_token_reports_key_address_and_plaintext(tmp_path) -> None:
    person = PersonStore(tmp_path / "persons.json").create(display_name="张三")
    stub = _StubTokenService(
        issued=IssuedToken(
            key="zhangsan",
            participant_id="api:zhangsan",
            token="sk-abc123",
            person=None,
        )
    )
    tool, _ = _token_tool(tmp_path, stub)
    result = await tool.execute(
        action="issue_token", person_id=person.person_id, note="office IDE"
    )
    assert not result.is_error
    assert "zhangsan" in result.content
    assert "api:zhangsan" in result.content
    assert "sk-abc123" in result.content
    kind, target, kwargs = stub.calls[0]
    assert (kind, target) == ("issue", person.person_id)
    assert kwargs["origin"] == "agent"
    assert kwargs["note"] == "office IDE"


async def test_issue_token_without_service_reports_unavailable(tmp_path) -> None:
    store = PersonStore(tmp_path / "persons.json")
    tool = PersonaTool(store, PersonaCard())
    result = await tool.execute(action="issue_token")
    assert result.is_error
    assert "尚未就绪" in result.content or "not ready yet" in result.content


async def test_issue_token_surfaces_service_error(tmp_path) -> None:
    stub = _StubTokenService(error=ModelApiTokenError(403, "模型接口未启用"))
    tool, _ = _token_tool(tmp_path, stub)
    result = await tool.execute(action="issue_token", person_id="p_x")
    assert result.is_error
    assert "模型接口未启用" in result.content


async def test_token_actions_require_person_id(tmp_path) -> None:
    stub = _StubTokenService()
    tool, _ = _token_tool(tmp_path, stub)
    for action in ("issue_token", "revoke_token", "list_tokens"):
        result = await tool.execute(action=action)
        assert result.is_error
        assert "person_id" in result.content
    assert stub.calls == []


async def test_revoke_token_requires_key(tmp_path) -> None:
    stub = _StubTokenService()
    tool, _ = _token_tool(tmp_path, stub)
    result = await tool.execute(action="revoke_token", person_id="p_x")
    assert result.is_error
    assert "key" in result.content
    assert stub.calls == []


async def test_revoke_token_reports_key_and_address(tmp_path) -> None:
    stub = _StubTokenService()
    tool, _ = _token_tool(tmp_path, stub)
    result = await tool.execute(action="revoke_token", person_id="p_x", key="alice")
    assert not result.is_error
    assert "alice" in result.content
    assert "api:alice" in result.content
    kind, target, kwargs = stub.calls[0]
    assert (kind, target, kwargs["key"], kwargs["origin"]) == (
        "revoke",
        "p_x",
        "alice",
        "agent",
    )


async def test_list_tokens_without_key_lists_summaries_only(tmp_path) -> None:
    stub = _StubTokenService(
        summaries=[
            TokenSummary(key="alice", participant_id="api:alice", display_name="Alice", note="ide"),
            TokenSummary(key="car", participant_id="api:car", display_name="Alice", note=""),
        ]
    )
    tool, _ = _token_tool(tmp_path, stub)
    result = await tool.execute(action="list_tokens", person_id="p_x")
    assert not result.is_error
    assert "alice" in result.content and "car" in result.content
    assert "sk-" not in result.content  # 摘要不含明文
    kind, target, _ = stub.calls[0]
    assert (kind, target) == ("list", "p_x")


async def test_list_tokens_with_key_returns_plaintext(tmp_path) -> None:
    stub = _StubTokenService(
        detail=TokenDetail(
            key="alice",
            participant_id="api:alice",
            display_name="Alice",
            note="ide",
            token="sk-xyz789",
        )
    )
    tool, _ = _token_tool(tmp_path, stub)
    result = await tool.execute(action="list_tokens", person_id="p_x", key="alice")
    assert not result.is_error
    assert "sk-xyz789" in result.content
    kind, target, kwargs = stub.calls[0]
    assert (kind, target, kwargs["key"], kwargs["origin"]) == (
        "read",
        "p_x",
        "alice",
        "agent",
    )


async def test_issue_token_rejects_multiline_note(tmp_path) -> None:
    stub = _StubTokenService()
    tool, _ = _token_tool(tmp_path, stub)
    result = await tool.execute(
        action="issue_token", person_id="p_x", note="office IDE\nsecond line"
    )
    assert result.is_error
    assert "单行" in result.content or "single line" in result.content
    assert stub.calls == []  # 校验失败不触达服务
