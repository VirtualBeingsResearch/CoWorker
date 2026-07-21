from __future__ import annotations

import pytest
from scripts import check_version


@pytest.mark.parametrize(
    ("ref_name", "expected"),
    [
        ("v0.3.2", "0.3.2"),
        ("v1.0.0-rc.1", "1.0.0-rc.1"),
    ],
)
def test_release_tag_version_accepts_canonical_tags(ref_name: str, expected: str) -> None:
    assert check_version.release_tag_version("tag", ref_name) == expected


@pytest.mark.parametrize(
    "ref_name",
    [
        "0.3.2",
        "coworker-desktop-v0.3.2",
        "v0.3",
        "v0.3.2+build.1",
    ],
)
def test_release_tag_version_rejects_noncanonical_tags(ref_name: str) -> None:
    with pytest.raises(ValueError, match="release tag must match"):
        check_version.release_tag_version("tag", ref_name)


def test_release_tag_version_ignores_branch_names_that_start_with_v() -> None:
    assert check_version.release_tag_version("branch", "v0.3.2") is None


def test_main_rejects_tag_that_does_not_match_project_version(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GITHUB_REF_TYPE", "tag")
    monkeypatch.setenv("GITHUB_REF_NAME", "v99.99.99")

    assert check_version.main() == 1
    assert "GITHUB_REF_NAME: expected" in capsys.readouterr().err


def test_main_does_not_treat_a_branch_as_a_release_tag(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_REF_TYPE", "branch")
    monkeypatch.setenv("GITHUB_REF_NAME", "v99.99.99")

    assert check_version.main() == 0
