from __future__ import annotations

import json

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
