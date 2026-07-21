from __future__ import annotations

import os
import platform
import sys

from coworker.i18n import locale_context
from coworker.identity.identity import Identity
from coworker.palaces.loader import PalaceLoader
from coworker.prompts.system_prompt import SystemPromptBuilder
from coworker.skills.loader import SkillLoader
from coworker.tools.file_tools import ReadFileTool
from coworker.tools.registry import ToolRegistry


def make_builder(tmp_path, with_name: bool = False) -> SystemPromptBuilder:
    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()
    if with_name:
        (identity_dir / "name.txt").write_text("Luna", encoding="utf-8")

    identity = Identity(str(identity_dir))
    identity.load()

    registry = ToolRegistry()
    registry.register(ReadFileTool())

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_dir = skills_dir / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: test\n---\nSkill body.", encoding="utf-8"
    )

    skill_loader = SkillLoader(str(skills_dir))
    thinking_path = tmp_path / "thinking.md"
    return SystemPromptBuilder(identity, registry, skill_loader, thinking_path=thinking_path)


def make_builder_with_thinking(tmp_path, thinking_text: str) -> SystemPromptBuilder:
    builder = make_builder(tmp_path)
    thinking_path = tmp_path / "thinking.md"
    thinking_path.write_text(thinking_text, encoding="utf-8")
    builder._thinking_path = thinking_path
    return builder


class TestSystemPromptBuilder:
    def test_build_contains_required_sections(self, tmp_path):
        builder = make_builder(tmp_path)
        prompt = builder.build()
        assert "[IDENTITY]" in prompt
        assert "[INSTINCTS]" in prompt
        assert "[GUIDELINES]" in prompt
        assert "[TOOLS]" not in prompt

    def test_build_newborn_identity(self, tmp_path):
        builder = make_builder(tmp_path, with_name=False)
        prompt = builder.build()
        assert "我是一个刚刚诞生的虚拟生命体" in prompt
        assert "还没有名字" in prompt
        assert "你是一个刚刚诞生的虚拟生命体" not in prompt

    def test_build_named_identity(self, tmp_path):
        builder = make_builder(tmp_path, with_name=True)
        prompt = builder.build()
        assert "我的名字是 **Luna**" in prompt
        assert "Luna" in prompt

    def test_build_instincts_use_first_person_self_frame(self, tmp_path):
        builder = make_builder(tmp_path)
        prompt = builder.build()
        assert "以下六条深层本能是我生存, 生活的依赖" in prompt
        assert "遇到不懂的事情、问题、事物，我会自然地想去搜索、探索、学习" in prompt
        assert "你有以下六条深层本能" not in prompt

    def test_build_includes_skills(self, tmp_path):
        builder = make_builder(tmp_path)
        prompt = builder.build()
        assert "[SKILLS]" in prompt
        assert "test-skill" in prompt

    def test_build_prompts_not_includes_tools(self, tmp_path):
        builder = make_builder(tmp_path)
        prompt = builder.build()
        assert "read_file" not in prompt

    def test_build_guidelines_mention_get_context(self, tmp_path):
        builder = make_builder(tmp_path)
        prompt = builder.build()
        assert "get_context" in prompt

    def test_build_contains_environment_section(self, tmp_path):
        builder = make_builder(tmp_path)
        prompt = builder.build()
        assert "[ENVIRONMENT]" in prompt

    def test_build_environment_shows_os(self, tmp_path):
        builder = make_builder(tmp_path)
        prompt = builder.build()
        assert platform.system() in prompt

    def test_build_environment_shows_python_version(self, tmp_path):
        builder = make_builder(tmp_path)
        prompt = builder.build()
        assert sys.version.split()[0] in prompt

    def test_build_environment_shows_cwd(self, tmp_path):
        builder = make_builder(tmp_path)
        prompt = builder.build()
        assert os.getcwd() in prompt

    def test_build_environment_shows_architecture(self, tmp_path):
        builder = make_builder(tmp_path)
        prompt = builder.build()
        assert platform.machine() in prompt

    def test_build_environment_shows_timezone(self, tmp_path):
        builder = make_builder(tmp_path)
        prompt = builder.build()
        assert "时区" in prompt
        assert "UTC" in prompt

    def test_build_reuses_cached_prompt_until_refresh(self, tmp_path):
        builder = make_builder(tmp_path)
        first = builder.build()

        (builder._identity._dir / "name.txt").write_text("Luna", encoding="utf-8")
        (builder._skills._dir / "test-skill" / "SKILL.md").write_text(
            "---\nname: test-skill\ndescription: updated skill\n---\nSkill body.",
            encoding="utf-8",
        )

        cached = builder.build()
        assert cached == first
        assert "Luna" not in cached
        assert "updated skill" not in cached

        builder.refresh()
        refreshed = builder.build()
        assert refreshed != first
        assert "Luna" in refreshed
        assert "updated skill" in refreshed

    def test_english_prompt_is_complete_and_contains_reply_language_policy(self, tmp_path):
        builder = make_builder(tmp_path)
        with locale_context("en"):
            prompt = builder.build()
        assert "I am a newly born virtual being" in prompt
        assert "[LANGUAGE_POLICY]" in prompt
        assert "language of that participant's current message" in prompt
        assert "runtime locale: en" in prompt
        assert "[GUIDELINES]" in prompt

    def test_prompt_cache_is_partitioned_by_locale(self, tmp_path):
        builder = make_builder(tmp_path)
        with locale_context("zh-CN"):
            chinese = builder.build()
        with locale_context("en"):
            english = builder.build()
        with locale_context("zh-CN"):
            chinese_again = builder.build()

        assert chinese != english
        assert chinese_again is chinese
        assert len(builder._cached_prompts) == 2


class TestThinkingSnapshot:
    def test_build_includes_thinking_on_first_build(self, tmp_path):
        builder = make_builder_with_thinking(tmp_path, "初始思维模式")
        prompt = builder.build()
        assert "[THINKING]" in prompt
        assert "初始思维模式" in prompt

    def test_thinking_change_not_reflected_until_refresh(self, tmp_path):
        builder = make_builder_with_thinking(tmp_path, "旧思维")
        assert "旧思维" in builder.build()

        # 写盘修改后，未刷新前 build() 仍用缓存的旧内容
        builder._thinking_path.write_text("新思维", encoding="utf-8")
        prompt = builder.build()
        assert "旧思维" in prompt
        assert "新思维" not in prompt

        # 刷新后才反映新内容
        builder.refresh()
        prompt = builder.build()
        assert "新思维" in prompt
        assert "旧思维" not in prompt


def make_builder_with_palace(tmp_path) -> SystemPromptBuilder:
    builder = make_builder(tmp_path)
    palaces_dir = tmp_path / "palaces"
    palaces_dir.mkdir()
    pdir = palaces_dir / "product-bug"
    pdir.mkdir()
    (pdir / "PALACE.md").write_text(
        "---\nname: product-bug\nwhen_to_attach: 反馈示例产品缺陷时挂载\n"
        "critical_skills: [bug-create]\nmemory_tags: [product]\n---\n卡片正文不应进注册表",
        encoding="utf-8",
    )
    builder._palaces = PalaceLoader(str(palaces_dir))
    return builder


class TestPalacesSection:
    def test_no_palaces_section_when_loader_none(self, tmp_path):
        builder = make_builder(tmp_path)  # palace_loader defaults to None
        assert "[PALACES]" not in builder.build()

    def test_build_includes_palaces_registry(self, tmp_path):
        builder = make_builder_with_palace(tmp_path)
        prompt = builder.build()
        assert "[PALACES]" in prompt
        assert "product-bug" in prompt
        assert "反馈示例产品缺陷时挂载" in prompt

    def test_palaces_registry_excludes_card_body(self, tmp_path):
        """Resident registry stays thin (cache-stable): name + when_to_attach only."""
        builder = make_builder_with_palace(tmp_path)
        prompt = builder.build()
        assert "卡片正文不应进注册表" not in prompt

    def test_palaces_section_mentions_bubble_spawn(self, tmp_path):
        builder = make_builder_with_palace(tmp_path)
        prompt = builder.build()
        assert "bubble_spawn" in prompt
        assert "palaces" in prompt
