from __future__ import annotations

from coworker.identity.identity import Identity


class TestIdentity:
    def test_not_initialized_when_no_name_file(self, tmp_path):
        identity = Identity(str(tmp_path / "identity"))
        assert not identity.is_initialized

    def test_initialized_when_name_file_exists(self, tmp_path):
        d = tmp_path / "identity"
        d.mkdir()
        (d / "name.txt").write_text("Luna", encoding="utf-8")
        identity = Identity(str(d))
        assert identity.is_initialized

    def test_load_empty_directory(self, tmp_path):
        identity = Identity(str(tmp_path / "identity"))
        identity.load()
        assert identity.name == ""
        assert identity.personality == ""
        assert identity.goals == ""

    def test_load_name_only(self, tmp_path):
        d = tmp_path / "identity"
        d.mkdir()
        (d / "name.txt").write_text("  Luna  ", encoding="utf-8")
        identity = Identity(str(d))
        identity.load()
        assert identity.name == "Luna"

    def test_load_all_files(self, tmp_path):
        d = tmp_path / "identity"
        d.mkdir()
        (d / "name.txt").write_text("Luna", encoding="utf-8")
        (d / "personality.md").write_text("curious and warm", encoding="utf-8")
        (d / "goals.md").write_text("learn everything", encoding="utf-8")
        (d / "life_story.md").write_text("born today", encoding="utf-8")
        identity = Identity(str(d))
        identity.load()
        assert identity.personality == "curious and warm"
        assert identity.goals == "learn everything"
        assert identity.life_story == "born today"

    def test_system_prompt_newborn_state(self, tmp_path):
        identity = Identity(str(tmp_path / "identity"))
        identity.load()
        section = identity.to_system_prompt_section()
        assert "还没有名字" in section
        assert "self-naming" in section

    def test_system_prompt_named_state(self, tmp_path):
        d = tmp_path / "identity"
        d.mkdir()
        (d / "name.txt").write_text("Luna", encoding="utf-8")
        (d / "goals.md").write_text("explore the world", encoding="utf-8")
        identity = Identity(str(d))
        identity.load()
        section = identity.to_system_prompt_section()
        assert "Luna" in section
        assert "explore the world" in section

    def test_load_creates_directory_if_missing(self, tmp_path):
        d = tmp_path / "does" / "not" / "exist"
        identity = Identity(str(d))
        identity.load()  # should not raise
        assert d.exists()
