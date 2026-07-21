from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_manual_release_creates_a_tag_and_dispatches_both_release_workflows() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert 'description: "Release tag to create (for example v0.3.2)"' in workflow
    assert 'git tag --annotate "$RELEASE_TAG"' in workflow
    assert "gh workflow run coworker-desktop-release.yml" in workflow
    assert "gh workflow run container-release.yml" in workflow
    assert '--ref "$RELEASE_TAG"' in workflow


def test_desktop_dispatch_from_a_tag_creates_a_release_draft() -> None:
    workflow = (
        ROOT / ".github/workflows/coworker-desktop-release.yml"
    ).read_text(encoding="utf-8")

    assert "if: ${{ success() && github.ref_type == 'tag' }}" in workflow
