from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from coworker.api import app as api_app
from coworker.core.config import Config
from coworker.core.config_export import build_config_bundle, load_effective_config


@pytest.fixture
def client():
    api_app._inbox = None
    api_app._channel_system = None
    api_app._collector = None
    return TestClient(api_app.app)


def _admin_headers(monkeypatch, token: str = "secret-token") -> dict[str, str]:
    monkeypatch.setattr(
        api_app,
        "_desktop_updates_config",
        lambda: api_app.DesktopUpdatesConfig(dir="data/desktop_updates", admin_token=token),
    )
    return {"Authorization": f"Bearer {token}"}


def _write_fixture_tree(tmp_path) -> None:
    (tmp_path / "data" / "identity").mkdir(parents=True)
    (tmp_path / "data" / "identity" / "profile.json").write_text("{}", encoding="utf-8")
    (tmp_path / "data" / "thinking.md").write_text("# thinking", encoding="utf-8")
    (tmp_path / ".coworker" / "skills" / "demo").mkdir(parents=True)
    (tmp_path / ".coworker" / "skills" / "demo" / "SKILL.md").write_text(
        "demo skill", encoding="utf-8"
    )
    (tmp_path / ".coworker" / "skills" / "demo" / "reference.txt").write_text(
        "skill reference", encoding="utf-8"
    )
    (tmp_path / ".coworker" / "palaces" / "demo").mkdir(parents=True)
    (tmp_path / ".coworker" / "palaces" / "demo" / "PALACE.md").write_text(
        "demo palace", encoding="utf-8"
    )
    (tmp_path / ".coworker" / "palaces" / "demo" / "notes.json").write_text(
        "{}", encoding="utf-8"
    )
    (tmp_path / ".coworker" / "subconscious" / "meta").mkdir(parents=True)
    (tmp_path / ".coworker" / "subconscious" / "meta" / "MODE.md").write_text(
        "meta mode", encoding="utf-8"
    )
    (tmp_path / ".coworker" / "plugins" / "demo").mkdir(parents=True)
    (tmp_path / ".coworker" / "plugins" / "demo" / "plugin.json").write_text(
        "{}", encoding="utf-8"
    )
    (tmp_path / "providers.json").write_text("[]", encoding="utf-8")


class TestExportConfigAuth:
    def test_missing_token_returns_401(self, client, monkeypatch):
        _admin_headers(monkeypatch)
        resp = client.get("/api/export_config")
        assert resp.status_code == 401

    def test_wrong_token_returns_403(self, client, monkeypatch):
        _admin_headers(monkeypatch)
        resp = client.get(
            "/api/export_config",
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 403

    def test_token_not_configured_returns_503(self, client, monkeypatch):
        monkeypatch.setattr(
            api_app,
            "_desktop_updates_config",
            lambda: api_app.DesktopUpdatesConfig(dir="data/desktop_updates", admin_token=""),
        )
        resp = client.get(
            "/api/export_config",
            headers={"Authorization": "Bearer whatever"},
        )
        assert resp.status_code == 503


class TestExportConfigEndpoint:
    def test_export_returns_zip_with_data_skills_palaces_providers(
        self, client, monkeypatch, tmp_path
    ):
        headers = _admin_headers(monkeypatch)
        _write_fixture_tree(tmp_path)
        monkeypatch.chdir(tmp_path)

        resp = client.get("/api/export_config", headers=headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            names = set(zf.namelist())
            assert "config.json" in names
            assert "data/identity/profile.json" in names
            assert "data/thinking.md" in names
            assert ".coworker/skills/demo/SKILL.md" in names
            assert ".coworker/skills/demo/reference.txt" in names
            assert ".coworker/palaces/demo/PALACE.md" in names
            assert ".coworker/palaces/demo/notes.json" in names
            assert ".coworker/subconscious/meta/MODE.md" in names
            assert ".coworker/plugins/demo/plugin.json" in names
            assert "providers.json" in names

            rebuilt = Config.model_validate(json.loads(zf.read("config.json")))
            assert rebuilt.agent.identity_dir == "data/identity"


class TestBuildConfigBundle:
    def test_packs_full_data_dir_and_rebuilds_equivalent_config(self, monkeypatch, tmp_path):
        _write_fixture_tree(tmp_path)
        monkeypatch.chdir(tmp_path)

        config = load_effective_config()
        dest = tmp_path / "out" / "bundle.zip"
        build_config_bundle(config, dest)

        assert dest.is_file()
        with zipfile.ZipFile(dest) as zf:
            names = set(zf.namelist())
            assert "data/identity/profile.json" in names
            assert "data/thinking.md" in names
            assert ".coworker/skills/demo/SKILL.md" in names
            assert ".coworker/skills/demo/reference.txt" in names
            assert ".coworker/palaces/demo/PALACE.md" in names
            assert ".coworker/palaces/demo/notes.json" in names
            assert ".coworker/subconscious/meta/MODE.md" in names
            assert ".coworker/plugins/demo/plugin.json" in names
            assert "providers.json" in names

            rebuilt = Config.model_validate_json(zf.read("config.json"))
            assert rebuilt.model_dump() == config.model_dump()

    def test_missing_optional_trees_are_skipped_without_error(self, monkeypatch, tmp_path):
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "marker.txt").write_text("x", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        config = load_effective_config()
        dest = tmp_path / "bundle.zip"
        build_config_bundle(config, dest)

        with zipfile.ZipFile(dest) as zf:
            names = set(zf.namelist())
            assert "data/marker.txt" in names
            assert not any(n.startswith(".coworker/") for n in names)
            assert "providers.json" not in names
