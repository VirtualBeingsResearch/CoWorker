from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_prepare_release_bumps_version_and_opens_a_pull_request() -> None:
    workflow = (ROOT / ".github/workflows/prepare-release.yml").read_text(
        encoding="utf-8"
    )

    assert 'description: "Release version without the v prefix' in workflow
    assert 'python scripts/bump_version.py "$RELEASE_VERSION"' in workflow
    assert "Validate release pull request token" in workflow
    assert "Missing RELEASE_PR_TOKEN" in workflow
    assert "GH_TOKEN: ${{ secrets.RELEASE_PR_TOKEN }}" in workflow
    assert "gh pr create" in workflow
    assert 'gh workflow run ci.yml --repo "$GITHUB_REPOSITORY"' in workflow
    assert "Refusing generated change outside the version file allowlist" in workflow


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
