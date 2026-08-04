"""The standalone ``persona`` tool: bind addresses, record notes, merge people.

Kept separate from ``manage_memory`` so the persona mechanism can be validated
on its own before (optionally) being folded into the memory tools.
"""

from __future__ import annotations

from typing import Any

from coworker.core.types import ToolResult
from coworker.i18n import tr
from coworker.persona import PersonaCard, PersonAlias, PersonStore
from coworker.tools.base import Tool, ToolDefinition


def _normalize_note(note: str) -> tuple[str, bool]:
    """Collapse whitespace runs (including newlines) into single spaces.

    Notes are single-line by design — the card renders one note per line, so a
    multi-line note would break the framework. Returns the cleaned note and
    whether it changed.
    """
    cleaned = " ".join(note.split())
    return cleaned, cleaned != note


class PersonaTool(Tool):
    """Bind addresses to persons, record notes, render cards, merge persons."""

    def __init__(self, store: PersonStore, cards: PersonaCard) -> None:
        self._store = store
        self._cards = cards

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="persona",
            description="管理「人物」：绑定通信地址与备注（bind）、记录个性化备注（note）、读人物画像框架（card）、解绑地址（unbind）、删除人物（delete）、合并重复人物（merge）",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["bind", "card", "note", "unbind", "delete", "merge"],
                        "description": "操作类型：bind（绑定地址/地址备注）、card（读画像框架）、note（记录/移除人物备注）、unbind（解除地址绑定）、delete（删除人物）、merge（合并人物）",
                    },
                    "participant_id": {
                        "type": "string",
                        "description": "通信地址（bind 时必填）",
                    },
                    "conversation_id": {
                        "type": "string",
                        "description": "该地址下的会话 ID（bind 时可选；同一真人按会话区分时带上，如微信的 session）",
                    },
                    "person_id": {
                        "type": "string",
                        "description": "人物 ID（card/note 时必填；bind 时指定要绑定到的已知人物）",
                    },
                    "name": {
                        "type": "string",
                        "description": "人物称呼（bind 且未指定 person_id 时：用于新建人物或按名匹配已有）",
                    },
                    "note": {
                        "type": "string",
                        "description": "备注内容：bind 时为该地址的备注（可多次追加）；note 时为人物级个性化备注",
                    },
                    "remove": {
                        "type": "boolean",
                        "description": "note 时可选：true = 移除该条备注（遗忘过时信息），缺省 = 追加",
                    },
                    "keep_person_id": {
                        "type": "string",
                        "description": "合并时保留的人物 ID（merge 时必填）",
                    },
                    "drop_person_id": {
                        "type": "string",
                        "description": "合并时被并入并删除的人物 ID（merge 时必填）",
                    },
                },
                "required": ["action"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action") or "")
        try:
            if action == "bind":
                return self._bind(kwargs)
            if action == "card":
                return self._card(kwargs)
            if action == "note":
                return self._note(kwargs)
            if action == "unbind":
                return self._unbind(kwargs)
            if action == "delete":
                return self._delete(kwargs)
            if action == "merge":
                return self._merge(kwargs)
        except Exception as e:
            return ToolResult(tool_call_id="", content=str(e), is_error=True)
        return ToolResult(
            tool_call_id="",
            content=tr("tool_result.common.unknown_action", action=action),
            is_error=True,
        )

    def _bind(self, kwargs: dict[str, Any]) -> ToolResult:
        participant_id = str(kwargs.get("participant_id") or "").strip()
        if not participant_id:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.bind_needs_participant"),
                is_error=True,
            )
        conversation_id = kwargs.get("conversation_id") or None
        if conversation_id is not None:
            conversation_id = str(conversation_id).strip() or None
        person_id = kwargs.get("person_id") or None
        name = kwargs.get("name") or None
        created = False

        if person_id:
            person = self._store.get(str(person_id))
            if person is None:
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.persona.person_missing", person_id=person_id),
                    is_error=True,
                )
        elif name:
            person = self._store.find_by_name(str(name))
            if person is None:
                person = self._store.create(display_name=str(name))
                created = True
            else:
                created = False
        else:
            person = self._store.create()
            created = True

        note = kwargs.get("note")
        if note and str(note).strip():
            cleaned, collapsed = _normalize_note(str(note))
            notes = [cleaned] if cleaned else []
        else:
            cleaned, collapsed = "", False
            notes = []
        alias = PersonAlias(
            participant_id=participant_id,
            conversation_id=conversation_id,
            notes=notes,
        )
        bound = self._store.bind_alias(person.person_id, alias)
        if bound is None:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.person_missing", person_id=person_id or ""),
                is_error=True,
            )
        display = person.display_name or person.person_id
        if created:
            content = tr(
                "tool_result.persona.bind_created",
                name=display,
                person_id=person.person_id,
                participant_id=participant_id,
            )
        else:
            content = tr(
                "tool_result.persona.bind_attached",
                participant_id=participant_id,
                name=display,
                person_id=person.person_id,
            )
        if collapsed:
            content = f"{content}\n{tr('tool_result.persona.note_single_line')}"
        return ToolResult(tool_call_id="", content=content)

    def _card(self, kwargs: dict[str, Any]) -> ToolResult:
        person_id = str(kwargs.get("person_id") or "").strip()
        if not person_id:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.card_needs_person"),
                is_error=True,
            )
        person = self._store.get(person_id)
        if person is None:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.person_missing", person_id=person_id),
                is_error=True,
            )
        card = self._cards.render(person)
        if not card:
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.persona.card_empty",
                    name=person.display_name or person.person_id,
                    person_id=person_id,
                ),
            )
        return ToolResult(tool_call_id="", content=card)

    def _note(self, kwargs: dict[str, Any]) -> ToolResult:
        person_id = str(kwargs.get("person_id") or "").strip()
        note = str(kwargs.get("note") or "").strip()
        if not person_id:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.note_needs_person"),
                is_error=True,
            )
        if not note:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.note_needs_content"),
                is_error=True,
            )
        cleaned, collapsed = _normalize_note(note)
        if not cleaned:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.note_needs_content"),
                is_error=True,
            )
        person = self._store.get(person_id)
        if person is None:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.person_missing", person_id=person_id),
                is_error=True,
            )
        remove = bool(kwargs.get("remove"))
        if remove:
            self._store.remove_note(person_id, cleaned)
            content = tr(
                "tool_result.persona.note_removed",
                name=person.display_name or person.person_id,
                person_id=person_id,
                note=cleaned,
            )
        else:
            self._store.add_note(person_id, cleaned)
            content = tr(
                "tool_result.persona.note_added",
                name=person.display_name or person.person_id,
                person_id=person_id,
                note=cleaned,
            )
        if collapsed:
            content = f"{content}\n{tr('tool_result.persona.note_single_line')}"
        return ToolResult(tool_call_id="", content=content)

    def _unbind(self, kwargs: dict[str, Any]) -> ToolResult:
        person_id = str(kwargs.get("person_id") or "").strip()
        participant_id = str(kwargs.get("participant_id") or "").strip()
        if not person_id:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.unbind_needs_person"),
                is_error=True,
            )
        if not participant_id:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.unbind_needs_address"),
                is_error=True,
            )
        person = self._store.get(person_id)
        if person is None:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.person_missing", person_id=person_id),
                is_error=True,
            )
        conversation_id = kwargs.get("conversation_id") or None
        if conversation_id is not None:
            conversation_id = str(conversation_id).strip() or None
        if not self._store.unbind_alias(person_id, participant_id, conversation_id):
            return ToolResult(
                tool_call_id="",
                content=tr(
                    "tool_result.persona.unbind_not_found",
                    participant_id=participant_id,
                ),
                is_error=True,
            )
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.persona.unbound",
                participant_id=participant_id,
                name=person.display_name or person.person_id,
                person_id=person.person_id,
            ),
        )

    def _delete(self, kwargs: dict[str, Any]) -> ToolResult:
        person_id = str(kwargs.get("person_id") or "").strip()
        if not person_id:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.delete_needs_person"),
                is_error=True,
            )
        person = self._store.get(person_id)
        if person is None:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.person_missing", person_id=person_id),
                is_error=True,
            )
        display = person.display_name or person.person_id
        self._store.delete(person_id)
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.persona.deleted",
                name=display,
                person_id=person_id,
            ),
        )

    def _merge(self, kwargs: dict[str, Any]) -> ToolResult:
        keep_id = str(kwargs.get("keep_person_id") or "").strip()
        drop_id = str(kwargs.get("drop_person_id") or "").strip()
        if not keep_id or not drop_id:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.merge_needs_both"),
                is_error=True,
            )
        keep = self._store.get(keep_id)
        drop = self._store.get(drop_id)
        if keep is None or drop is None or keep_id == drop_id:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.merge_invalid"),
                is_error=True,
            )
        merged = self._store.merge(keep_id, drop_id)
        if merged is None:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.merge_invalid"),
                is_error=True,
            )
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.persona.merge_done",
                drop_name=drop.display_name or drop.person_id,
                drop_id=drop.person_id,
                keep_name=keep.display_name or keep.person_id,
                keep_id=keep.person_id,
            ),
        )
