from __future__ import annotations

from scripts import bump_version


def test_changelog_range_matches_new_and_legacy_release_tags(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_git_output(root, args):
        assert root == tmp_path
        calls.append(args)
        return "v0.3.1"

    monkeypatch.setattr(bump_version, "git_output", fake_git_output)

    assert bump_version.changelog_range(tmp_path) == "v0.3.1..HEAD"
    assert calls == [
        [
            "describe",
            "--tags",
            "--match",
            "v[0-9]*",
            "--match",
            "coworker-desktop-v[0-9]*",
            "--abbrev=0",
        ]
    ]


def test_update_uv_lock_updates_coworker_package(tmp_path, monkeypatch) -> None:
    path = tmp_path / "uv.lock"
    path.write_text(
        '[[package]]\nname = "coworker"\nversion = "0.2.0"\n\n'
        '[[package]]\nname = "dependency"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)

    bump_version.update_uv_lock(path, "0.2.1")

    assert path.read_text(encoding="utf-8") == (
        '[[package]]\nname = "coworker"\nversion = "0.2.1"\n\n'
        '[[package]]\nname = "dependency"\nversion = "1.0.0"\n'
    )


def test_update_cargo_lock_updates_workspace_packages(tmp_path, monkeypatch) -> None:
    path = tmp_path / "Cargo.lock"
    path.write_text(
        '[[package]]\nname = "coworker-desktop-app"\nversion = "0.2.0"\n\n'
        '[[package]]\nname = "dependency"\nversion = "0.2.0"\n\n'
        '[[package]]\nname = "coworker-desktop-core"\nversion = "0.2.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)

    bump_version.update_cargo_lock(path, "0.2.1")

    assert path.read_text(encoding="utf-8") == (
        '[[package]]\nname = "coworker-desktop-app"\nversion = "0.2.1"\n\n'
        '[[package]]\nname = "dependency"\nversion = "0.2.0"\n\n'
        '[[package]]\nname = "coworker-desktop-core"\nversion = "0.2.1"\n'
    )


def test_upsert_changelog_section_inserts_after_title() -> None:
    text = "# Changelog\n\n## 0.1.0 - Unreleased\n\n- Initial release.\n"

    result = bump_version.upsert_changelog_section(text, "0.2.0", "- feat: add bridge")

    assert result == (
        "# Changelog\n\n"
        "## 0.2.0 - Unreleased\n\n"
        "- feat: add bridge\n\n"
        "## 0.1.0 - Unreleased\n\n"
        "- Initial release.\n"
    )


def test_upsert_changelog_section_replaces_todo_body() -> None:
    text = (
        "# Changelog\n\n"
        "## 0.2.0 - Unreleased\n\n"
        "- TODO\n\n"
        "## 0.1.0 - Unreleased\n\n"
        "- Initial release.\n"
    )

    result = bump_version.upsert_changelog_section(text, "0.2.0", "- fix: repair updater")

    assert result == (
        "# Changelog\n\n"
        "## 0.2.0 - Unreleased\n\n"
        "- fix: repair updater\n\n"
        "## 0.1.0 - Unreleased\n\n"
        "- Initial release.\n"
    )


def test_upsert_changelog_section_preserves_manual_body() -> None:
    text = "# Changelog\n\n## 0.2.0 - Unreleased\n\n- 手写发布说明\n"

    assert bump_version.upsert_changelog_section(text, "0.2.0", "- feat: add bridge") == text


def test_upsert_changelog_section_moves_generic_unreleased_body_to_version() -> None:
    text = (
        "# Changelog\n\n"
        "## Unreleased\n\n"
        "- 手写发布说明\n\n"
        "## 0.1.0 - 2026-01-01\n\n"
        "- Initial release.\n"
    )

    result = bump_version.upsert_changelog_section(text, "0.2.0", "- feat: generated")

    assert result == (
        "# Changelog\n\n"
        "## Unreleased\n\n"
        "## 0.2.0 - Unreleased\n\n"
        "- 手写发布说明\n\n"
        "## 0.1.0 - 2026-01-01\n\n"
        "- Initial release.\n"
    )


def test_upsert_changelog_section_fills_empty_generic_unreleased_body() -> None:
    text = "# Changelog\n\n## Unreleased\n\n## 0.1.0 - 2026-01-01\n\n- Initial.\n"

    result = bump_version.upsert_changelog_section(text, "0.2.0", "- feat: generated")

    assert result == (
        "# Changelog\n\n"
        "## Unreleased\n\n"
        "## 0.2.0 - Unreleased\n\n"
        "- feat: generated\n\n"
        "## 0.1.0 - 2026-01-01\n\n"
        "- Initial.\n"
    )


def test_normalize_changelog_subject_filters_release_noise() -> None:
    assert (
        bump_version.normalize_changelog_subject(
            "Pull request #91: optimize(subconscious): 优化审计，针对长期任务优化"
        )
        == "optimize(subconscious): 优化审计，针对长期任务优化"
    )
    assert bump_version.normalize_changelog_subject("chore(release): bump version to 0.2.0") is None
    assert (
        bump_version.normalize_changelog_subject("world_model: update morning progress")
        == "world_model: update morning progress"
    )
    assert (
        bump_version.normalize_changelog_subject("chore(world_model): add trace note")
        == "chore(world_model): add trace note"
    )
    assert (
        bump_version.normalize_changelog_subject("thinking.md: add a working principle")
        == "thinking.md: add a working principle"
    )
    assert (
        bump_version.normalize_changelog_subject("fix(testing): add local discipline")
        == "fix(testing): add local discipline"
    )
