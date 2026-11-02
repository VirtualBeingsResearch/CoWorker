from __future__ import annotations

import json

import pytest

from coworker.core.config import Config
from coworker.core.model_config import (
    RuntimeModelConfig,
    RuntimePreferenceState,
    runtime_preference_revision,
)
from coworker.core.startup_intent import (
    clear_startup_intent,
    load_bootstrap_startup_intent,
    write_bootstrap_startup_intent,
)


def test_bootstrap_startup_intent_persists_until_cleared(tmp_path):
    path = write_bootstrap_startup_intent(
        tmp_path,
        provider="openai",
        model="custom-tool-model",
        reconnect_proof="ab" * 32,
    )

    intent = load_bootstrap_startup_intent(
        tmp_path,
        provider="openai",
        model="custom-tool-model",
        available_providers={"openai"},
    )

    assert intent is not None
    assert intent.reason == "bootstrap"
    assert intent.provider == "openai"
    assert intent.model == "custom-tool-model"
    assert intent.reconnect_proof == "ab" * 32
    assert path.exists()
    assert "api_key" not in path.read_text(encoding="utf-8")

    clear_startup_intent(tmp_path)
    assert not path.exists()


def test_bootstrap_startup_intent_discards_mismatched_config(tmp_path):
    path = write_bootstrap_startup_intent(
        tmp_path,
        provider="openai",
        model="gpt-5.2",
    )

    intent = load_bootstrap_startup_intent(
        tmp_path,
        provider="openai",
        model="gpt-5.5",
        available_providers={"openai"},
    )

    assert intent is None
    assert not path.exists()


def test_bootstrap_startup_intent_discards_unavailable_provider(tmp_path):
    path = write_bootstrap_startup_intent(
        tmp_path,
        provider="openai",
        model="gpt-5.2",
    )

    intent = load_bootstrap_startup_intent(
        tmp_path,
        provider="openai",
        model="gpt-5.2",
        available_providers=set(),
    )

    assert intent is None
    assert not path.exists()


def test_bootstrap_startup_intent_discards_invalid_payload(tmp_path):
    path = tmp_path / "startup_intent.json"
    path.write_text(json.dumps({"version": 99, "reason": "bootstrap"}), encoding="utf-8")

    intent = load_bootstrap_startup_intent(
        tmp_path,
        provider="openai",
        model="gpt-5.2",
        available_providers={"openai"},
    )

    assert intent is None
    assert not path.exists()


@pytest.mark.parametrize("fields", [
    {"preference_revision": "not-a-revision"},
    {"preference_revision": 12},
    {"preference_revision": "ab" * 32, "selected_at": ""},
    {"preference_revision": "ab" * 32, "selected_at": "2026-11-02T10:00:00"},
    {"selected_at": 123},
    {"selected_at": "not-a-timestamp"},
])
def test_bootstrap_receipt_rejects_invalid_link_or_timestamp(tmp_path, fields):
    path = write_bootstrap_startup_intent(tmp_path, provider="openai", model="gpt-5.2")
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**payload, **fields}), encoding="utf-8")
    assert load_bootstrap_startup_intent(
        tmp_path, provider="openai", model="gpt-5.2", available_providers={"openai"},
    ) is None
    assert not path.exists()


def test_legacy_bootstrap_marker_remains_readable(tmp_path):
    path = tmp_path / "startup_intent.json"
    path.write_text(json.dumps({"version": 1, "reason": "bootstrap", "provider": "openai", "model": "gpt-5.2"}), encoding="utf-8")
    intent = load_bootstrap_startup_intent(
        tmp_path, provider="openai", model="gpt-5.2", available_providers={"openai"},
    )
    assert intent is not None
    assert intent.preference_revision is None and intent.selected_at == ""


def test_bootstrap_revision_tracks_selections_instead_of_policy_or_aliases(tmp_path):
    llm = Config(_env_file=None).llm
    runtime = RuntimeModelConfig(
        preferred={"provider": "opencode-go", "model": "deepseek-v4-flash"},
        preference=RuntimePreferenceState.explicit(),
    )
    revision = runtime_preference_revision(runtime, llm)
    runtime.preferred.model = "deepseek-v4.1-flash"
    runtime.fallbacks = ["backup/backup-model"]
    runtime.routing = runtime.routing.model_copy(update={"cooldown_seconds": 120})
    assert runtime_preference_revision(runtime, llm) == revision
    runtime.preference = RuntimePreferenceState.explicit()
    assert runtime_preference_revision(runtime, llm) != revision


def test_bootstrap_marker_matches_a_migrated_model_id(tmp_path):
    from coworker.core.model_migrations import migrate_provider_model_reference

    llm = Config(_env_file=None).llm
    path = write_bootstrap_startup_intent(
        tmp_path, provider="opencode-go", model="deepseek-v4-flash", preference_revision="ab" * 32,
    )
    intent = load_bootstrap_startup_intent(
        tmp_path, provider="opencode-go", model="deepseek-v4.1-flash", available_providers={"opencode-go"},
        model_resolver=lambda provider, model: migrate_provider_model_reference(llm, provider, model),
    )
    assert intent is not None and intent.model == "deepseek-v4.1-flash"
    assert path.exists()
