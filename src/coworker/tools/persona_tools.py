"""The standalone ``persona`` tool: bind addresses, record notes, merge people.

Kept separate from ``manage_memory`` so the persona mechanism can be validated
on its own before (optionally) being folded into the memory tools.
"""

from __future__ import annotations

from typing import Any

from coworker.channels.modelapi.tokens import (
    ModelApiTokenError,
    ModelApiTokenService,
)
from coworker.core.types import ToolResult
from coworker.i18n import tr
from coworker.persona import PersonaCard, PersonAlias, PersonStore
from coworker.tools.base import Tool, ToolDefinition


def _note_is_multiline(note: str) -> bool:
    """Notes are single-line by design — the card renders one note per line."""
    return "\n" in note or "\r" in note


class PersonaTool(Tool):
    """Bind addresses to persons, record notes, render cards, merge persons."""

    def __init__(
        self,
        store: PersonStore,
        cards: PersonaCard,
        tokens: ModelApiTokenService | None = None,
    ) -> None:
        self._store = store
        self._cards = cards
        self._tokens = tokens

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="persona",
            description="管理「人物」：绑定通信地址与备注（bind）、记录个性化备注（note）、读人物画像框架（card）、解绑地址（unbind）、删除人物（delete）、合并重复人物（merge）、签发模型接口令牌（issue_token）、撤销模型接口令牌（revoke_token）、列出令牌或按 key 取回明文（list_tokens）",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "bind",
                            "card",
                            "note",
                            "unbind",
                            "delete",
                            "merge",
                            "issue_token",
                            "revoke_token",
                            "list_tokens",
                        ],
                        "description": "操作类型：bind（绑定地址/地址备注）、card（读画像框架）、note（记录/移除人物备注）、unbind（解除地址绑定）、delete（删除人物）、merge（合并人物）、issue_token（签发模型接口令牌）、revoke_token（撤销模型接口令牌）、list_tokens（列出令牌；带 key 时返回该令牌明文）",
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
                        "description": "人物 ID（card/note 时必填；bind 时指定要绑定到的已知人物；issue_token/revoke_token/list_tokens 时必填）",
                    },
                    "name": {
                        "type": "string",
                        "description": "人物称呼（bind 且未指定 person_id 时：用于新建人物或按名匹配已有）",
                    },
                    "note": {
                        "type": "string",
                        "description": "该地址的备注（bind 时可选；同一地址可多次 bind 追加多条备注）；issue_token 时作为令牌备注（可选，记录哪个应用或设备在用）。必须为单行文本，不能包含换行",
                    },
                    "key": {
                        "type": "string",
                        "description": "令牌键名：issue_token 时可选（缺省从人物名派生）；revoke_token 时必填；list_tokens 时可选（指定则返回该令牌的明文）",
                    },
                    "notes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "人物级备注列表（note 时必填）：一次添加或移除多条，每条必须为单行文本，不能包含换行",
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
            if action == "issue_token":
                return await self._issue_token(kwargs)
            if action == "revoke_token":
                return await self._revoke_token(kwargs)
            if action == "list_tokens":
                return self._list_tokens(kwargs)
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

        # 校验先行：多行备注直接报错，不产生任何副作用。
        note = kwargs.get("note")
        if note and str(note).strip():
            cleaned_note = str(note).strip()
            if _note_is_multiline(cleaned_note):
                return ToolResult(
                    tool_call_id="",
                    content=tr("tool_result.persona.note_must_be_single_line"),
                    is_error=True,
                )
        else:
            cleaned_note = ""

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

        notes = [cleaned_note] if cleaned_note else []
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

    def _collect_notes(self, kwargs: dict[str, Any]) -> tuple[list[str], str | None]:
        """Collect notes from the ``notes`` parameter (a list, or a single string).

        Returns ``(notes, error_key)``: ``error_key`` is a tr key when any note
        is multi-line — nothing is written in that case.
        """
        notes_param = kwargs.get("notes")
        if isinstance(notes_param, list):
            raw = [str(n) for n in notes_param]
        elif notes_param:
            raw = [str(notes_param)]
        else:
            raw = []
        cleaned = [n.strip() for n in raw if n and n.strip()]
        for n in cleaned:
            if _note_is_multiline(n):
                return [], "tool_result.persona.note_must_be_single_line"
        return cleaned, None

    def _note(self, kwargs: dict[str, Any]) -> ToolResult:
        person_id = str(kwargs.get("person_id") or "").strip()
        if not person_id:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.note_needs_person"),
                is_error=True,
            )
        notes, error_key = self._collect_notes(kwargs)
        if error_key:
            return ToolResult(
                tool_call_id="",
                content=tr(error_key),
                is_error=True,
            )
        if not notes:
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
            for note in notes:
                self._store.remove_note(person_id, note)
            content = tr(
                "tool_result.persona.note_removed",
                name=person.display_name or person.person_id,
                person_id=person_id,
                count=len(notes),
            )
        else:
            for note in notes:
                self._store.add_note(person_id, note)
            content = tr(
                "tool_result.persona.note_added",
                name=person.display_name or person.person_id,
                person_id=person_id,
                count=len(notes),
            )
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

    def _display_name(self, person_id: str) -> str:
        person = self._store.get(person_id)
        if person is None:
            return person_id
        return person.display_name or person.person_id

    def _token_error(self, error: ModelApiTokenError) -> ToolResult:
        return ToolResult(tool_call_id="", content=str(error.detail), is_error=True)

    def _token_target(self, kwargs: dict[str, Any], action: str) -> tuple[str, str] | ToolResult:
        """Validate the common ``person_id``/``key`` pair; ``key`` may be empty."""
        if self._tokens is None:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.token_unavailable"),
                is_error=True,
            )
        person_id = str(kwargs.get("person_id") or "").strip()
        if not person_id:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.token_needs_person", action=action),
                is_error=True,
            )
        key = str(kwargs.get("key") or "").strip()
        return person_id, key

    async def _issue_token(self, kwargs: dict[str, Any]) -> ToolResult:
        target = self._token_target(kwargs, "issue_token")
        if isinstance(target, ToolResult):
            return target
        person_id, key = target
        assert self._tokens is not None
        note = str(kwargs.get("note") or "").strip()
        if note and _note_is_multiline(note):
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.note_must_be_single_line"),
                is_error=True,
            )
        try:
            issued = await self._tokens.issue(
                person_id, note=note, key=key, origin="agent"
            )
        except ModelApiTokenError as error:
            return self._token_error(error)
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.persona.token_issued",
                name=self._display_name(person_id),
                person_id=person_id,
                key=issued.key,
                participant_id=issued.participant_id,
                token=issued.token,
            ),
        )

    async def _revoke_token(self, kwargs: dict[str, Any]) -> ToolResult:
        target = self._token_target(kwargs, "revoke_token")
        if isinstance(target, ToolResult):
            return target
        person_id, key = target
        assert self._tokens is not None
        if not key:
            return ToolResult(
                tool_call_id="",
                content=tr("tool_result.persona.token_needs_key", action="revoke_token"),
                is_error=True,
            )
        try:
            await self._tokens.revoke(person_id, key, origin="agent")
        except ModelApiTokenError as error:
            return self._token_error(error)
        return ToolResult(
            tool_call_id="",
            content=tr(
                "tool_result.persona.token_revoked",
                name=self._display_name(person_id),
                person_id=person_id,
                key=key,
                participant_id=f"api:{key}",
            ),
        )

    def _list_tokens(self, kwargs: dict[str, Any]) -> ToolResult:
        target = self._token_target(kwargs, "list_tokens")
        if isinstance(target, ToolResult):
            return target
        person_id, key = target
        assert self._tokens is not None
        try:
            if key:
                detail = self._tokens.read_plaintext(person_id, key, origin="agent")
                content_key = (
                    "tool_result.persona.token_fetched"
                    if detail.note
                    else "tool_result.persona.token_fetched_plain"
                )
                content = tr(
                    content_key,
                    name=self._display_name(person_id),
                    person_id=person_id,
                    key=detail.key,
                    participant_id=detail.participant_id,
                    note=detail.note,
                    token=detail.token,
                )
            else:
                summaries = self._tokens.list_for_person(person_id)
                if not summaries:
                    content = tr(
                        "tool_result.persona.token_list_empty",
                        name=self._display_name(person_id),
                        person_id=person_id,
                    )
                else:
                    items = "\n".join(
                        tr(
                            (
                                "tool_result.persona.token_list_item"
                                if summary.note
                                else "tool_result.persona.token_list_item_plain"
                            ),
                            key=summary.key,
                            participant_id=summary.participant_id,
                            note=summary.note,
                        )
                        for summary in summaries
                    )
                    content = tr(
                        "tool_result.persona.token_list_header",
                        name=self._display_name(person_id),
                        person_id=person_id,
                        count=len(summaries),
                        items=items,
                    )
        except ModelApiTokenError as error:
            return self._token_error(error)
        return ToolResult(tool_call_id="", content=content)
