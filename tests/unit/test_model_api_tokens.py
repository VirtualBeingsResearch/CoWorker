"""Integration tests for the shared model API token service.

The service is the single write path behind both the admin persons page and
the agent's persona tool, so these tests exercise it against a real
``AdminConfigService`` plus a registered model API channel module: a patch
must hot-apply to the channel runtime directory and persist the override.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from coworker.admin.configuration import (
    AdminConfigDependencies,
    AdminConfigService,
)
from coworker.channels.modelapi import create_model_api_module
from coworker.channels.modelapi.tokens import (
    ModelApiTokenError,
    ModelApiTokenService,
)
from coworker.channels.module import ChannelModuleRegistry
from coworker.core.config import Config
from coworker.persona import PersonStore


def _service(tmp_path, *, enabled: bool = True):
    config = Config.model_validate(
        {
            "admin": {
                "token": "secret",
                "config_file": str(tmp_path / "admin_config.json"),
            },
            "memory": {"db_path": str(tmp_path / "memory")},
            "agent": {"logs_dir": str(tmp_path / "logs")},
            "model_api": {"enabled": enabled},
        }
    )
    agent = SimpleNamespace(_identity=SimpleNamespace(name="Luna"))
    brain = SimpleNamespace(active_provider=object())
    registry = ChannelModuleRegistry()
    module = create_model_api_module(config.model_api, tmp_path / "sessions.json")
    registry.register(module)
    admin_service = AdminConfigService(
        AdminConfigDependencies(
            agent=agent,
            brain=brain,
            config=config,
            inherited_config=config.model_copy(deep=True),
        )
    )
    admin_service.set_channel_modules(registry)
    store = PersonStore(tmp_path / "persons.json")
    tokens = ModelApiTokenService(
        config=config, person_store=store, config_service=admin_service
    )
    return tokens, store, module, config


async def test_issue_binds_alias_and_hot_applies_to_directory(tmp_path) -> None:
    tokens, store, module, _ = _service(tmp_path)
    person = store.create(display_name="Alice")

    issued = await tokens.issue(person.person_id, note="ide", origin="agent")

    assert issued.key == "alice"
    assert issued.participant_id == "api:alice"
    assert issued.token.startswith("sk-")
    identity = module.runtime.directory.resolve_authorization(
        f"Bearer {issued.token}"
    )
    assert identity is not None
    assert identity.participant_id == "api:alice"
    bound = {alias.participant_id for alias in store.get(person.person_id).aliases}
    assert "api:alice" in bound

    audit = (tmp_path / "logs" / "admin_audit.jsonl").read_text(encoding="utf-8")
    entry = json.loads(audit.strip().splitlines()[-1])
    assert entry["action"] == "person.model_api_token_issue"
    assert entry["source"] == "agent"
    assert entry["detail"] == "key=alice"


async def test_second_token_gets_distinct_address_and_secret(tmp_path) -> None:
    tokens, store, _, _ = _service(tmp_path)
    person = store.create(display_name="Alice")

    first = await tokens.issue(person.person_id)
    second = await tokens.issue(person.person_id)

    assert (first.key, second.key) == ("alice", "alice-2")
    assert first.token != second.token


async def test_issue_requires_existing_person(tmp_path) -> None:
    tokens, *_ = _service(tmp_path)
    with pytest.raises(ModelApiTokenError) as exc:
        await tokens.issue("p_missing")
    assert exc.value.status == 404


async def test_agent_issue_refused_while_disabled_admin_path_unaffected(tmp_path) -> None:
    tokens, store, _, _ = _service(tmp_path, enabled=False)
    person = store.create(display_name="Alice")

    with pytest.raises(ModelApiTokenError) as exc:
        await tokens.issue(person.person_id, origin="agent")
    assert exc.value.status == 403

    issued = await tokens.issue(person.person_id, origin="admin")
    assert issued.key == "alice"


async def test_revoke_kills_token_and_unbinds_address(tmp_path) -> None:
    tokens, store, module, _ = _service(tmp_path)
    person = store.create(display_name="Alice")
    issued = await tokens.issue(person.person_id, origin="admin")

    await tokens.revoke(person.person_id, issued.key, origin="agent")

    assert (
        module.runtime.directory.resolve_authorization(f"Bearer {issued.token}")
        is None
    )
    bound = {alias.participant_id for alias in store.get(person.person_id).aliases}
    assert f"api:{issued.key}" not in bound

    audit = (tmp_path / "logs" / "admin_audit.jsonl").read_text(encoding="utf-8")
    entry = json.loads(audit.strip().splitlines()[-1])
    assert entry["action"] == "person.model_api_token_revoke"
    assert entry["source"] == "agent"


async def test_agent_revoke_refused_for_unbound_key(tmp_path) -> None:
    tokens, store, _, _ = _service(tmp_path)
    owner = store.create(display_name="Bob")
    stranger = store.create(display_name="Alice")
    issued = await tokens.issue(owner.person_id, origin="admin")

    with pytest.raises(ModelApiTokenError) as exc:
        await tokens.revoke(stranger.person_id, issued.key, origin="agent")
    assert exc.value.status == 404


async def test_list_and_agent_plaintext_lookup(tmp_path) -> None:
    tokens, store, _, _ = _service(tmp_path)
    person = store.create(display_name="Alice")
    first = await tokens.issue(person.person_id, note="ide", origin="admin")
    second = await tokens.issue(person.person_id, key="car", origin="admin")

    summaries = tokens.list_for_person(person.person_id)
    assert [summary.key for summary in summaries] == ["alice", "car"]
    assert summaries[0].note == "ide"
    assert summaries[1].participant_id == "api:car"

    detail = tokens.read_plaintext(person.person_id, "car", origin="agent")
    assert detail.token == second.token
    assert detail.token != first.token


async def test_agent_plaintext_lookup_refused_for_unbound_key(tmp_path) -> None:
    tokens, store, _, _ = _service(tmp_path)
    owner = store.create(display_name="Bob")
    stranger = store.create(display_name="Alice")
    issued = await tokens.issue(owner.person_id, origin="admin")

    with pytest.raises(ModelApiTokenError) as exc:
        tokens.read_plaintext(stranger.person_id, issued.key, origin="agent")
    assert exc.value.status == 404


async def test_set_note_updates_existing_entry(tmp_path) -> None:
    tokens, store, _, _ = _service(tmp_path)
    person = store.create(display_name="Alice")
    issued = await tokens.issue(person.person_id, origin="admin")

    note = await tokens.set_note(person.person_id, issued.key, note="office IDE")

    assert note == "office IDE"
    assert tokens.read_plaintext(person.person_id, issued.key).note == "office IDE"


def test_unattached_service_reports_unavailable(tmp_path) -> None:
    config = Config.model_validate(
        {
            "admin": {
                "token": "secret",
                "config_file": str(tmp_path / "admin_config.json"),
            },
            "memory": {"db_path": str(tmp_path / "memory")},
            "agent": {"logs_dir": str(tmp_path / "logs")},
        }
    )
    store = PersonStore(tmp_path / "persons.json")
    person = store.create(display_name="Alice")
    tokens = ModelApiTokenService(config=config, person_store=store)

    with pytest.raises(ModelApiTokenError) as exc:
        tokens.list_for_person(person.person_id)
    assert exc.value.status == 503
