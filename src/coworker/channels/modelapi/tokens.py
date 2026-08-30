"""Shared model API token lifecycle for the admin console and the persona tool.

Tokens are always issued against an existing person record: the participant
address is derived from the token key and bound to the person up front, so the
first message lands on a known identity instead of creating a duplicate person.
One write path serves both surfaces — the admin persons page and the agent's
``persona`` tool: issuance patches ``model_api.tokens`` through the admin
config service (which persists the secret and hot-applies to the channel
runtime).

The agent path is capability-scoped: it may only touch tokens whose
participant address is bound to the target person, and enabling the feature
stays with the administrator — issuing is refused while ``model_api.enabled``
is false. Agent writes append to the same ``admin_audit.jsonl`` with
``source: "agent"`` so the console audit view shows them.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from coworker.admin.configuration import ConfigUpdate, ConfigUpdateError, JsonObject
from coworker.channels.modelapi.sessions import token_key_slug
from coworker.i18n import tr
from coworker.persona import Person, PersonAlias

if TYPE_CHECKING:
    from coworker.admin.configuration import AdminConfigService
    from coworker.core.config import Config
    from coworker.persona import PersonStore

type TokenOrigin = Literal["admin", "agent"]

_MODEL_API_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# 与管理台 ModelApiTokenIssuePayload / ModelApiTokenNotePayload 的上限一致。
_TOKEN_NOTE_MAX = 200


class ModelApiTokenError(Exception):
    """A token operation failed; ``detail`` is a rendered, localized message."""

    def __init__(self, status: int, detail: object) -> None:
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class IssuedToken:
    """A freshly issued token; ``token`` is the one-time plaintext."""

    key: str
    participant_id: str
    token: str
    person: Person | None


@dataclass(frozen=True)
class TokenSummary:
    """One issued token without its secret."""

    key: str
    participant_id: str
    display_name: str
    note: str


@dataclass(frozen=True)
class TokenDetail(TokenSummary):
    """One token including its plaintext (issue-time or explicit lookup)."""

    token: str


class ModelApiTokenService:
    """Issue, revoke, list, and look up model API tokens for persons."""

    def __init__(
        self,
        *,
        config: Config,
        person_store: PersonStore,
        config_service: AdminConfigService | None = None,
        logs_dir: str | Path | None = None,
    ) -> None:
        self._config = config
        self._store = person_store
        self._config_service = config_service
        self._logs_dir = (
            Path(logs_dir) if logs_dir is not None else Path(config.agent.logs_dir)
        )

    def attach(self, config_service: AdminConfigService | None) -> None:
        """Late-bind the admin config service (it is created after tool wiring)."""
        self._config_service = config_service

    async def issue(
        self,
        person_id: str,
        *,
        note: str = "",
        key: str = "",
        origin: TokenOrigin = "admin",
    ) -> IssuedToken:
        person = self._require_person(person_id)
        service = self._require_service()
        if origin == "agent" and not self._config.model_api.enabled:
            raise ModelApiTokenError(403, tr("tool_result.persona.token_disabled"))
        cleaned_note = note.strip()
        if len(cleaned_note) > _TOKEN_NOTE_MAX:
            raise ModelApiTokenError(
                422,
                tr("tool_result.persona.token_note_too_long", limit=_TOKEN_NOTE_MAX),
            )
        snapshot_tokens = self._snapshot_tokens(service)

        base = token_key_slug(key) or token_key_slug(
            person.display_name or person.person_id
        )
        if not base:
            base = f"p{secrets.token_hex(4)}"
        elif not _MODEL_API_KEY_RE.fullmatch(base):
            base = f"k{base}"
        final_key = base
        suffix = 2
        while final_key in snapshot_tokens:
            final_key = f"{base}-{suffix}"
            suffix += 1

        token_value = f"sk-{secrets.token_urlsafe(24)}"
        # Config section merges are shallow, so the patch must carry the full
        # token dict; secret values of existing entries are preserved by the
        # config service, and the new entry's secret rides in `secrets`.
        entries = self._token_entries(snapshot_tokens)
        entries[final_key] = {
            "display_name": person.display_name,
            "note": cleaned_note,
        }
        await self._patch(
            service,
            ConfigUpdate(
                changes={"model_api": {"tokens": entries}},
                secrets={f"model_api.tokens.{final_key}.token": token_value},
            ),
        )
        participant_id = f"api:{final_key}"
        updated = self._store.bind_alias(
            person_id,
            PersonAlias(participant_id=participant_id, channel="model-api"),
        )
        if origin == "agent":
            self._audit(
                "person.model_api_token_issue", person_id, detail=f"key={final_key}"
            )
        return IssuedToken(
            key=final_key,
            participant_id=participant_id,
            token=token_value,
            person=updated,
        )

    async def revoke(
        self,
        person_id: str,
        key: str,
        *,
        origin: TokenOrigin = "admin",
    ) -> Person | None:
        person = self._require_person(person_id)
        service = self._require_service()
        snapshot_tokens = self._snapshot_tokens(service)
        if key not in snapshot_tokens or (
            origin == "agent" and f"api:{key}" not in self._bound_addresses(person)
        ):
            raise ModelApiTokenError(404, tr("api.model_api.token_not_found", key=key))
        entries = self._token_entries(snapshot_tokens)
        del entries[key]
        await self._patch(
            service, ConfigUpdate(changes={"model_api": {"tokens": entries}})
        )
        self._store.unbind_alias(person_id, f"api:{key}")
        if origin == "agent":
            self._audit(
                "person.model_api_token_revoke", person_id, detail=f"key={key}"
            )
        return self._store.get(person_id)

    def list_for_person(self, person_id: str) -> list[TokenSummary]:
        """Summaries of the person's tokens (no secrets), keyed by binding.

        A token belongs to a person iff its ``api:<key>`` address is bound to
        that person; tokens seeded through ``.env`` show up once bound.
        """
        person = self._require_person(person_id)
        service = self._require_service()
        bound = self._bound_addresses(person)
        summaries: list[TokenSummary] = []
        for token_key, entry in self._snapshot_tokens(service).items():
            if not isinstance(entry, dict):
                continue
            participant_id = f"api:{token_key}"
            if participant_id not in bound:
                continue
            summaries.append(
                TokenSummary(
                    key=str(token_key),
                    participant_id=participant_id,
                    display_name=str(entry.get("display_name") or ""),
                    note=str(entry.get("note") or ""),
                )
            )
        return summaries

    def read_plaintext(
        self,
        person_id: str,
        key: str,
        *,
        origin: TokenOrigin = "admin",
    ) -> TokenDetail:
        """Return one token's plaintext for copy workflows, like the admin GET."""
        person = self._require_person(person_id)
        entry = self._config.model_api.tokens.get(key)
        if entry is None or (
            origin == "agent" and f"api:{key}" not in self._bound_addresses(person)
        ):
            raise ModelApiTokenError(404, tr("api.model_api.token_not_found", key=key))
        return TokenDetail(
            key=key,
            participant_id=f"api:{key}",
            display_name=entry.display_name,
            note=entry.note,
            token=entry.token,
        )

    async def set_note(
        self,
        person_id: str,
        key: str,
        *,
        note: str,
        origin: TokenOrigin = "admin",
    ) -> str:
        """Update an issued token's admin remark (which app or device it serves)."""
        self._require_person(person_id)
        service = self._require_service()
        cleaned_note = note.strip()
        if len(cleaned_note) > _TOKEN_NOTE_MAX:
            raise ModelApiTokenError(
                422,
                tr("tool_result.persona.token_note_too_long", limit=_TOKEN_NOTE_MAX),
            )
        entries = self._token_entries(self._snapshot_tokens(service))
        entry = entries.get(key)
        if not isinstance(entry, dict):
            raise ModelApiTokenError(404, tr("api.model_api.token_not_found", key=key))
        entry["note"] = cleaned_note
        await self._patch(
            service, ConfigUpdate(changes={"model_api": {"tokens": entries}})
        )
        return cleaned_note

    def _require_person(self, person_id: str) -> Person:
        person = self._store.get(person_id)
        if person is None:
            raise ModelApiTokenError(
                404, tr("api.admin.person_missing", person_id=person_id)
            )
        return person

    def _require_service(self) -> AdminConfigService:
        if self._config_service is None:
            raise ModelApiTokenError(
                503, tr("tool_result.persona.token_unavailable")
            )
        return self._config_service

    @staticmethod
    def _snapshot_tokens(service: AdminConfigService) -> JsonObject:
        """Token entries from the merged desired config (keys stay authoritative)."""
        model_api = service.snapshot().config.get("model_api")
        tokens = model_api.get("tokens") if isinstance(model_api, dict) else None
        return tokens if isinstance(tokens, dict) else {}

    @staticmethod
    def _token_entries(snapshot_tokens: JsonObject) -> JsonObject:
        """Full-dict patch payload preserving every existing entry's remark."""
        entries: JsonObject = {}
        for token_key, entry in snapshot_tokens.items():
            if isinstance(entry, dict):
                entries[str(token_key)] = {
                    "display_name": str(entry.get("display_name") or ""),
                    "note": str(entry.get("note") or ""),
                }
        return entries

    @staticmethod
    def _bound_addresses(person: Person) -> set[str]:
        return {alias.participant_id for alias in person.aliases}

    @staticmethod
    async def _patch(service: AdminConfigService, update: ConfigUpdate) -> None:
        try:
            await service.patch(update)
        except ConfigUpdateError as error:
            raise ModelApiTokenError(error.status_code, error.detail) from error

    def _audit(self, action: str, target: str, *, detail: str = "") -> None:
        """Append the admin-console audit line, marked as agent-originated."""
        entry = {
            "ts": datetime.now().isoformat(),
            "action": action,
            "target": target,
            "result": "ok",
            "source": "agent",
            "detail": detail[:500],
        }
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        with (self._logs_dir / "admin_audit.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
