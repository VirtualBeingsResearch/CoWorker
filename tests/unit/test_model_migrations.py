from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coworker.application import _migrate_short_term_active_model
from coworker.core.config import Config, apply_admin_config_file
from coworker.core.model_config import apply_runtime_model_config_file
from coworker.core.model_migrations import (
    migrate_model_configuration,
    migrate_model_id,
    migrate_provider_model_reference,
)


def _load_config(
    tmp_path: Path,
    *,
    admin: dict | None = None,
    llm: dict | None = None,
) -> tuple[Config, Config, Path]:
    admin_path = tmp_path / "admin_config.json"
    if admin is not None:
        admin_path.write_text(json.dumps(admin, ensure_ascii=False), encoding="utf-8")
    inherited = Config(
        _env_file=None,
        admin={"config_file": str(admin_path)},
        **({"llm": llm} if llm is not None else {}),
    )
    return inherited, apply_admin_config_file(inherited.model_copy(deep=True)), admin_path


@pytest.mark.parametrize(
    ("provider_type", "model_id", "expected"),
    [
        ("opencode-go", "deepseek-flash", "deepseek-v4.1-flash"),
        ("opencode-go", "deepseek-v4-flash", "deepseek-v4.1-flash"),
        ("opencode-go", "deepseek-v4-flash-vision-exp", "deepseek-v4.1-flash"),
        ("opencode-go", "deepseek-v4.1-flash", "deepseek-v4.1-flash"),
        ("opencode-go", "deepseek-v4-pro", "deepseek-v4-pro"),
        ("deepseek", "deepseek-flash", "deepseek-flash"),
        ("", "deepseek-flash", "deepseek-flash"),
    ],
)
def test_migrate_model_id_is_provider_scoped(
    provider_type: str,
    model_id: str,
    expected: str,
) -> None:
    assert migrate_model_id(provider_type, model_id) == expected


def test_migrate_model_configuration_rewrites_all_references(tmp_path: Path) -> None:
    admin = {
        "llm": {
            "default_provider": "opencode-go",
            "default_model": "deepseek-flash",
            "summary_model": "deepseek-flash",
            "vision_provider": "opencode-go",
            "vision_model": "deepseek-v4-flash",
            "fallbacks": ["opencode-go/deepseek-flash"],
            "managed_providers": [
                {
                    "name": "opencode-go",
                    "type": "opencode-go",
                    "api_key": "k",
                    "default_model": "deepseek-flash",
                }
            ],
        }
    }
    inherited, config, admin_path = _load_config(tmp_path, admin=admin)

    migrations = migrate_model_configuration(config, inherited)

    assert config.llm.default_model == "deepseek-v4.1-flash"
    assert config.llm.summary_model == "deepseek-v4.1-flash"
    assert config.llm.vision_model == "deepseek-v4.1-flash"
    assert config.llm.fallbacks == ["opencode-go/deepseek-v4.1-flash"]
    assert config.llm.managed_providers[0].default_model == "deepseek-v4.1-flash"
    assert {migration.scope for migration in migrations} == {
        "provider",
        "default",
        "summary",
        "vision",
        "fallback",
    }

    saved = json.loads(admin_path.read_text(encoding="utf-8"))
    assert saved["llm"]["default_model"] == "deepseek-v4.1-flash"
    assert saved["llm"]["summary_model"] == "deepseek-v4.1-flash"
    assert saved["llm"]["vision_model"] == "deepseek-v4.1-flash"
    assert saved["llm"]["fallbacks"] == ["opencode-go/deepseek-v4.1-flash"]
    assert saved["llm"]["managed_providers"][0]["default_model"] == "deepseek-v4.1-flash"


def test_official_deepseek_flash_is_not_migrated(tmp_path: Path) -> None:
    admin = {
        "llm": {
            "default_provider": "deepseek",
            "default_model": "deepseek-flash",
            "summary_model": "deepseek-flash",
            "fallbacks": ["deepseek/deepseek-flash"],
        }
    }
    inherited, config, admin_path = _load_config(tmp_path, admin=admin)
    original_admin = admin_path.read_text(encoding="utf-8")

    migrations = migrate_model_configuration(config, inherited)

    assert migrations == []
    assert config.llm.default_model == "deepseek-flash"
    assert config.llm.summary_model == "deepseek-flash"
    assert config.llm.fallbacks == ["deepseek/deepseek-flash"]
    assert admin_path.read_text(encoding="utf-8") == original_admin


def test_named_opencode_go_provider_is_migrated(tmp_path: Path) -> None:
    admin = {
        "llm": {
            "default_provider": "opencode-work",
            "default_model": "deepseek-flash",
            "summary_provider": "opencode-work",
            "summary_model": "deepseek-flash",
            "fallbacks": ["opencode-work/deepseek-v4-flash"],
            "managed_providers": [
                {
                    "name": "opencode-work",
                    "type": "opencode-go",
                    "api_key": "k",
                    "default_model": "deepseek-v4-flash",
                }
            ],
        }
    }
    inherited, config, _ = _load_config(tmp_path, admin=admin)

    migrate_model_configuration(config, inherited)

    assert config.llm.default_model == "deepseek-v4.1-flash"
    assert config.llm.summary_model == "deepseek-v4.1-flash"
    assert config.llm.fallbacks == ["opencode-work/deepseek-v4.1-flash"]
    assert config.llm.managed_providers[0].default_model == "deepseek-v4.1-flash"


def test_summary_model_follows_the_active_provider(tmp_path: Path) -> None:
    inherited, config, _ = _load_config(
        tmp_path,
        admin={
            "llm": {
                "default_provider": "opencode-go",
                "default_model": "deepseek-v4.1-flash",
                "summary_model": "deepseek-flash",
            }
        },
    )

    migrate_model_configuration(config, inherited)

    assert config.llm.summary_model == "deepseek-v4.1-flash"


def test_summary_model_for_official_deepseek_is_unchanged(tmp_path: Path) -> None:
    inherited, config, _ = _load_config(
        tmp_path,
        admin={
            "llm": {
                "default_provider": "deepseek",
                "default_model": "deepseek-flash",
                "summary_model": "deepseek-flash",
            }
        },
    )

    migrate_model_configuration(config, inherited)

    assert config.llm.summary_model == "deepseek-flash"


def test_provider_file_default_model_is_migrated_in_memory(tmp_path: Path) -> None:
    providers_path = tmp_path / "providers.json"
    providers_path.write_text(
        json.dumps(
            [
                {
                    "name": "opencode-file",
                    "type": "opencode-go",
                    "api_key": "k",
                    "default_model": "deepseek-flash",
                }
            ]
        ),
        encoding="utf-8",
    )
    inherited, config, _ = _load_config(
        tmp_path,
        llm={"providers_file": str(providers_path)},
    )

    migrate_model_configuration(config, inherited)

    specs = {spec.name: spec for spec in config.llm.resolved_providers()}
    assert specs["opencode-file"].default_model == "deepseek-v4.1-flash"
    assert json.loads(providers_path.read_text(encoding="utf-8"))[0]["default_model"] == (
        "deepseek-flash"
    )


def test_runtime_model_config_file_is_rewritten(tmp_path: Path) -> None:
    runtime_path = tmp_path / "model_runtime_config.json"
    runtime_path.write_text(
        json.dumps(
            {
                "summary": {"provider": "opencode-go", "model": "deepseek-flash"},
                "fallbacks": ["opencode-go/deepseek-v4-flash"],
                "vision": {"provider": "opencode-go", "model": "deepseek-flash"},
            }
        ),
        encoding="utf-8",
    )
    inherited, config, _ = _load_config(
        tmp_path,
        llm={
            "default_provider": "opencode-go",
            "default_model": "deepseek-flash",
            "runtime_config_file": str(runtime_path),
        },
    )
    apply_runtime_model_config_file(config.llm)

    migrate_model_configuration(config, inherited)

    saved = json.loads(runtime_path.read_text(encoding="utf-8"))
    assert saved["summary"]["model"] == "deepseek-v4.1-flash"
    assert saved["fallbacks"] == ["opencode-go/deepseek-v4.1-flash"]
    assert saved["vision"]["model"] == "deepseek-v4.1-flash"


def test_migration_is_idempotent(tmp_path: Path) -> None:
    inherited, config, admin_path = _load_config(
        tmp_path,
        admin={
            "llm": {
                "default_provider": "opencode-go",
                "default_model": "deepseek-flash",
            }
        },
    )

    first = migrate_model_configuration(config, inherited)
    saved_after_first = admin_path.read_text(encoding="utf-8")
    second = migrate_model_configuration(config, inherited)

    assert first
    assert second == []
    assert admin_path.read_text(encoding="utf-8") == saved_after_first


def test_runtime_overrides_do_not_leak_into_admin_overrides(tmp_path: Path) -> None:
    runtime_path = tmp_path / "model_runtime_config.json"
    runtime_path.write_text(
        json.dumps(
            {
                "summary": {"provider": "opencode-go", "model": "deepseek-flash"},
                "fallbacks": ["opencode-go/deepseek-flash"],
            }
        ),
        encoding="utf-8",
    )
    admin = {
        "llm": {
            "default_provider": "deepseek",
            "default_model": "deepseek-flash",
            "summary_provider": "deepseek",
            "summary_model": "deepseek-chat",
            "fallbacks": ["deepseek/deepseek-flash"],
        }
    }
    inherited, config, admin_path = _load_config(
        tmp_path,
        admin=admin,
        llm={"runtime_config_file": str(runtime_path)},
    )
    apply_runtime_model_config_file(config.llm)
    original_admin = admin_path.read_text(encoding="utf-8")

    migrate_model_configuration(config, inherited)

    assert admin_path.read_text(encoding="utf-8") == original_admin
    saved_runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    assert saved_runtime["summary"]["model"] == "deepseek-v4.1-flash"
    assert saved_runtime["fallbacks"] == ["opencode-go/deepseek-v4.1-flash"]


def test_admin_values_are_persisted_even_when_runtime_already_has_new_id(
    tmp_path: Path,
) -> None:
    runtime_path = tmp_path / "model_runtime_config.json"
    runtime_path.write_text(
        json.dumps(
            {
                "summary": {
                    "provider": "opencode-go",
                    "model": "deepseek-v4.1-flash",
                }
            }
        ),
        encoding="utf-8",
    )
    admin = {
        "llm": {
            "default_provider": "opencode-go",
            "default_model": "deepseek-v4.1-flash",
            "summary_provider": "opencode-go",
            "summary_model": "deepseek-flash",
        }
    }
    inherited, config, admin_path = _load_config(
        tmp_path,
        admin=admin,
        llm={"runtime_config_file": str(runtime_path)},
    )
    apply_runtime_model_config_file(config.llm)

    migrations = migrate_model_configuration(config, inherited)

    assert migrations == []
    saved = json.loads(admin_path.read_text(encoding="utf-8"))
    assert saved["llm"]["summary_model"] == "deepseek-v4.1-flash"


def test_provider_model_reference_helper_uses_provider_type(tmp_path: Path) -> None:
    inherited, config, _ = _load_config(
        tmp_path,
        admin={
            "llm": {
                "default_provider": "opencode-work",
                "default_model": "deepseek-v4.1-flash",
                "managed_providers": [
                    {
                        "name": "opencode-work",
                        "type": "opencode-go",
                        "api_key": "k",
                    }
                ],
            }
        },
    )

    assert (
        migrate_provider_model_reference(config.llm, "opencode-work", "deepseek-flash")
        == "deepseek-v4.1-flash"
    )
    assert (
        migrate_provider_model_reference(config.llm, "deepseek", "deepseek-flash")
        == "deepseek-flash"
    )


def test_snapshot_active_model_is_migrated_on_restore(tmp_path: Path) -> None:
    _, config, _ = _load_config(
        tmp_path,
        admin={
            "llm": {
                "default_provider": "opencode-go",
                "default_model": "deepseek-v4.1-flash",
            }
        },
    )
    short_term = SimpleNamespace(
        active_provider="opencode-go",
        active_model="deepseek-flash",
    )

    _migrate_short_term_active_model(short_term, config)

    assert short_term.active_model == "deepseek-v4.1-flash"


def test_official_deepseek_snapshot_model_is_unchanged(tmp_path: Path) -> None:
    _, config, _ = _load_config(
        tmp_path,
        admin={
            "llm": {
                "default_provider": "deepseek",
                "default_model": "deepseek-flash",
            }
        },
    )
    short_term = SimpleNamespace(
        active_provider="deepseek",
        active_model="deepseek-flash",
    )

    _migrate_short_term_active_model(short_term, config)

    assert short_term.active_model == "deepseek-flash"
