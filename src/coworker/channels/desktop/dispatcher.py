"""Programmatic dispatch of CoWorker Desktop envelopes at the inbox boundary.

The desktop bridge POSTs ``DesktopEnvelopeV1`` envelopes to ``/messages``.
``routes.py`` unwraps ``desktop.thread.event`` to plain chat text, but every
other desktop event used to reach the agent as a ``json.dumps(envelope)``
string that the LLM had to parse by hand.

``DesktopDispatcher`` is wired as the ``InboxWatcher`` interceptor. It parses
the envelope once and routes by ``type``:

* ``desktop.actor.snapshot``           -> delegate to ``DesktopRegistry`` (consume)
* ``desktop.command.result`` (ok:true) -> consume (ack suppression)
* ``desktop.server_request.resolved``  -> consume (control-plane ack)
* ``desktop.command.result`` (ok:false)/``desktop.error`` -> render short text (wake)
* ``desktop.approval.requested``       -> render structured prompt + reply template (wake)
* ``desktop.user_input.requested``     -> render questions + answers template (wake)
* ``desktop.thread.event``             -> defensive: extract ``payload.message`` (wake)

Returning ``True`` consumes the event (the agent is never woken); returning
``False`` after rewriting ``event.content`` hands the agent clean text instead
of raw JSON. Actor-specific reply identifiers (Codex ``server_request_id`` vs
Claude ``request_id``) are resolved here so the rendered template is always
copy-pasteable and the skill no longer has to teach the difference.

Rendered prompts that exceed ``_FOLD_THRESHOLD`` are folded: the full text is
persisted via ``DesktopRegistry.write_detail`` (keyed by request/message id)
and the inline prompt keeps only a head of decision-essential lines plus a
``read_file`` pointer. The coworker reads the file on demand for full option
descriptions / command text instead of carrying the whole block in context.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger
from pydantic import BaseModel, ValidationError

from coworker.channels.desktop.registry import DesktopRegistry
from coworker.core.types import IncomingEvent
from coworker.i18n import tr

_DETAIL_LIMIT = 240

# A rendered desktop prompt longer than this is folded: the full text is
# persisted to the registry's detail store and the inline prompt keeps only
# the head (decision-essential lines) plus a ``read_file`` pointer.
_FOLD_THRESHOLD = 600
_FOLD_HEAD_CHARS = 400


class DesktopEnvelope(BaseModel):
    """Minimal typed view of a ``DesktopEnvelopeV1`` wire payload.

    Fields are permissive: the bridge emits polymorphic payloads, so anything
    beyond ``type``/``payload`` is optional and only used for rendering.
    """

    protocol_version: int | None = None
    message_id: str | None = None
    request_id: str | None = None
    conversation_id: str | None = None
    created_at: str | None = None
    type: str
    payload: dict[str, Any] | None = None


class DesktopDispatcher:
    """Inbox interceptor that dispatches desktop envelopes by ``type``."""

    def __init__(self, registry: DesktopRegistry) -> None:
        self._registry = registry

    def __call__(self, event: IncomingEvent) -> bool:
        envelope = self._parse(event.content)
        if envelope is None:
            # Plain chat text, a file attachment, or a non-desktop REST event.
            return False
        return self._route(event, envelope)

    @staticmethod
    def _parse(content: Any) -> DesktopEnvelope | None:
        if not isinstance(content, str) or not content:
            return None
        try:
            raw = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        event_type = raw.get("type")
        if not isinstance(event_type, str) or not event_type.startswith("desktop."):
            return None
        try:
            return DesktopEnvelope.model_validate(raw)
        except ValidationError:
            # Malformed desktop envelope: let it flow as-is rather than drop it.
            return None

    def _route(self, event: IncomingEvent, envelope: DesktopEnvelope) -> bool:
        if envelope.protocol_version not in (None, 1):
            logger.warning("Ignored unsupported CoWorker Desktop protocol envelope")
            return True

        event_type = envelope.type
        payload = envelope.payload or {}

        if event_type == "desktop.actor.snapshot":
            return self._registry.ingest_snapshot(payload, event.participant_id)
        if event_type == "desktop.command.result":
            if payload.get("ok") is True:
                return True
            event.content = self._render_error(envelope, tr("channel.desktop.command_rejected"))
            return False
        if event_type == "desktop.server_request.resolved":
            return True
        if event_type == "desktop.error":
            event.content = self._render_error(envelope, tr("channel.desktop.reported_error"))
            return False
        if event_type == "desktop.approval.requested":
            event.content = self._render_approval(envelope, event)
            return False
        if event_type == "desktop.user_input.requested":
            event.content = self._render_user_input(envelope, event)
            return False
        if event_type == "desktop.thread.event":
            # routes.py normally unwraps thread events to plain text before the
            # interceptor runs; this only fires if a thread envelope arrived raw.
            message = payload.get("message")
            if isinstance(message, str):
                event.content = self._maybe_fold(message, _detail_key(envelope))
            return False
        # Unknown desktop type: do not drop, let the agent see it as-is.
        return False

    # ------------------------------------------------------------------ render

    def _maybe_fold(self, text: str, key: str) -> str:
        """Fold ``text`` past the threshold, stashing the full content on disk.

        Short prompts pass through unchanged. Long ones are written to the
        registry's detail store (keyed by ``key``) and the inline prompt keeps
        a head of whole lines plus a ``read_file`` pointer to the full text.
        """
        if len(text) <= _FOLD_THRESHOLD:
            return text
        path = self._registry.write_detail(key, text)
        head = _head_lines(text, _FOLD_HEAD_CHARS)
        return f"{head}\n{tr('channel.desktop.folded', path=path)}"

    def _render_error(self, envelope: DesktopEnvelope, fallback: str) -> str:
        payload = envelope.payload or {}
        message = (
            payload.get("message") or payload.get("error") or payload.get("reason") or fallback
        )
        context = self._context_line(envelope)
        text = tr("channel.desktop.error", context=context, message=message)
        return self._maybe_fold(text, _detail_key(envelope))

    def _render_approval(self, envelope: DesktopEnvelope, event: IncomingEvent) -> str:
        payload = envelope.payload or {}
        actor, id_field, id_value = self._approval_identity(payload)
        tool = (
            payload.get("tool_name")
            or payload.get("method")
            or tr("channel.desktop.unknown_operation")
        )
        detail = self._summarize_detail(payload.get("input"), payload.get("params"))
        conversation_id = self._conversation_id(envelope, payload)
        context = self._context_line(envelope, actor=actor, conversation_id=conversation_id)

        extra_accept = _json_extra({id_field: id_value, "decision": "accept"})
        extra_decline = _json_extra({id_field: id_value, "decision": "decline"})
        note = ""
        method = payload.get("method")
        if isinstance(method, str) and method == "item/permissions/requestApproval":
            note = tr("channel.desktop.permission_note")

        return tr(
            "channel.desktop.approval",
            context=context,
            tool=tool,
            detail=detail,
            participant=event.participant_id,
            conversation=conversation_id,
            accept=extra_accept,
            decline=extra_decline,
            note=note,
        )

    def _render_user_input(self, envelope: DesktopEnvelope, event: IncomingEvent) -> str:
        payload = envelope.payload or {}
        actor, id_field, id_value = self._approval_identity(payload)
        conversation_id = self._conversation_id(envelope, payload)
        context = self._context_line(envelope, actor=actor, conversation_id=conversation_id)

        questions = _extract_questions(payload)
        if questions:
            # AskUserQuestion: the bridge's is_user_input branch expects
            # ``user_input_request_id`` (carrying the stored request_id) + answers.
            id_field = "user_input_request_id"
            header = [
                tr("channel.desktop.question_title"),
                context,
                tr("channel.desktop.answer_each"),
            ]
            reply_lines = self._user_input_reply_lines(event, conversation_id, id_field, id_value)
            full_text = "\n".join(
                header + _render_question_lines(questions, with_descriptions=True) + reply_lines
            )
            if len(full_text) <= _FOLD_THRESHOLD:
                return full_text
            # Too long: drop option descriptions inline (labels alone are enough
            # to answer) and stash the full listing for `read_file`.
            path = self._registry.write_detail(_detail_key(envelope), full_text)
            compact_text = "\n".join(
                header + _render_question_lines(questions, with_descriptions=False) + reply_lines
            )
            return f"{compact_text}\n{tr('channel.desktop.questions_folded', path=path)}"

        # Non-AskUserQuestion input request (e.g. Codex requestUserInput): the
        # bridge routes it through the approval path, so reply with decision.
        # Like approvals, it is bounded by ``_summarize_detail`` and actionable,
        # so it is not folded.
        detail = self._summarize_detail(payload.get("input"), payload.get("params"))
        extra_accept = _json_extra({id_field: id_value, "decision": "accept"})
        extra_decline = _json_extra({id_field: id_value, "decision": "decline"})
        return tr(
            "channel.desktop.input",
            context=context,
            detail=detail,
            participant=event.participant_id,
            conversation=conversation_id,
            accept=extra_accept,
            decline=extra_decline,
        )

    @staticmethod
    def _user_input_reply_lines(
        event: IncomingEvent, conversation_id: str, id_field: str, id_value: str
    ) -> list[str]:
        answers_example = _json_extra(
            {
                id_field: id_value,
                "answers": {
                    tr("channel.desktop.answer_question_example"): tr(
                        "channel.desktop.answer_value_example"
                    )
                },
            }
        )
        decline = _json_extra({id_field: id_value, "decision": "decline"})
        return [
            tr(
                "channel.desktop.answer_reply",
                participant=event.participant_id,
                conversation=conversation_id,
                answers=answers_example,
            ),
            tr("channel.desktop.answer_decline", decline=decline),
        ]

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _approval_identity(payload: dict[str, Any]) -> tuple[str, str, str]:
        """Return (actor_label, reply_id_field, reply_id_value) for a request.

        Codex approvals/user-inputs carry ``server_request_id``; Claude ones
        carry ``request_id``. The reply ``extra`` must use the matching field.
        """
        if payload.get("actor_id") == "claude":
            return "Claude", "request_id", str(payload.get("request_id") or "")
        return "Codex", "server_request_id", str(payload.get("server_request_id") or "")

    @staticmethod
    def _conversation_id(envelope: DesktopEnvelope, payload: dict[str, Any]) -> str:
        return (
            envelope.conversation_id
            or _as_str(payload.get("session_id"))
            or _as_str(payload.get("threadId"))
            or ""
        )

    @staticmethod
    def _context_line(
        envelope: DesktopEnvelope,
        *,
        actor: str | None = None,
        conversation_id: str | None = None,
    ) -> str:
        parts = []
        if actor:
            parts.append(tr("channel.desktop.context_source", actor=actor))
        cid = conversation_id if conversation_id is not None else envelope.conversation_id
        if cid:
            parts.append(tr("channel.desktop.context_conversation", conversation=cid))
        return (" ".join(parts) + "\n") if parts else ""

    @staticmethod
    def _summarize_detail(*candidates: Any) -> str:
        for candidate in candidates:
            if candidate is None:
                continue
            if isinstance(candidate, str):
                return _truncate(candidate)
            if isinstance(candidate, dict):
                for key in ("command", "cmd", "path", "file_path", "filePath", "pattern", "url"):
                    value = candidate.get(key)
                    if isinstance(value, str) and value:
                        return f"{key}={_truncate(value)}"
                return _truncate(json.dumps(candidate, ensure_ascii=False))
            return _truncate(json.dumps(candidate, ensure_ascii=False))
        return tr("channel.desktop.no_detail")


def _extract_questions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for container in (payload.get("input"), payload.get("params"), payload):
        if isinstance(container, dict):
            questions = container.get("questions")
            if isinstance(questions, list):
                return [q for q in questions if isinstance(q, dict)]
    return []


def _render_question_lines(
    questions: list[dict[str, Any]], *, with_descriptions: bool
) -> list[str]:
    lines: list[str] = []
    for index, question in enumerate(questions, start=1):
        question_text = question.get("question") or tr("channel.desktop.no_question")
        lines.append(f"{index}. {question_text}")
        options = question.get("options")
        if isinstance(options, list):
            for option in options:
                if isinstance(option, dict):
                    label = option.get("label", "")
                    if with_descriptions:
                        desc = option.get("description", "")
                        rendered = tr("channel.desktop.option", label=label, description=desc)
                        lines.append(rendered.rstrip(":："))
                    else:
                        lines.append(f"   - {label}")
    return lines


def _detail_key(envelope: DesktopEnvelope) -> str:
    return envelope.request_id or envelope.message_id or "desktop-detail"


def _head_lines(text: str, limit: int) -> str:
    """Return a leading slice of ``text`` within ``limit`` chars.

    Whole lines are kept while they fit. The first line that would overflow is
    truncated to a prefix (with ``…``) rather than dropped entirely, so a
    single huge line (e.g. a long error message) still leaves a visible
    prefix inline. Never returns empty for non-empty input.
    """
    kept: list[str] = []
    total = 0
    for line in text.split("\n"):
        addition = len(line) + 1  # +1 for the newline
        if total + addition > limit:
            remaining = limit - total
            if remaining > 1:
                kept.append(line[: remaining - 1].rstrip() + "…")
            break
        kept.append(line)
        total += addition
    return "\n".join(kept)


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _truncate(value: str) -> str:
    if len(value) <= _DETAIL_LIMIT:
        return value
    return value[: _DETAIL_LIMIT - 1] + "…"


def _json_extra(fields: dict[str, Any]) -> str:
    """Serialize an ``extra`` object for display inside a communicate() template."""
    return json.dumps({"operation": "resolve_request", **fields}, ensure_ascii=False)
