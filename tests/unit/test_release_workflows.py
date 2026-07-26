from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def workflow_text(name: str) -> str:
    return (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")


def test_prepare_release_bumps_version_and_opens_a_pull_request() -> None:
    workflow = workflow_text("prepare-release.yml")

    yaml.safe_load(workflow)
    assert 'description: "Release version without the v prefix' in workflow
    assert 'python scripts/bump_version.py "$RELEASE_VERSION"' in workflow
    assert "Validate release pull request token" in workflow
    assert "Missing RELEASE_PR_TOKEN" in workflow
    assert "GH_TOKEN: ${{ secrets.RELEASE_PR_TOKEN }}" in workflow
    assert "gh pr create" in workflow
    assert 'gh workflow run ci.yml --repo "$GITHUB_REPOSITORY"' in workflow
    assert "Refusing generated change outside the version file allowlist" in workflow
    assert "Build CoWorker Release Draft" in workflow
    assert "Publish CoWorker Release" in workflow


def test_draft_release_rejects_existing_tags_and_dispatches_a_candidate_build() -> None:
    workflow = workflow_text("draft-release.yml")

    yaml.safe_load(workflow)
    assert "Run this workflow from the default branch" in workflow
    assert "already exists and is immutable" in workflow
    assert "is already published and cannot be revised" in workflow
    assert "gh workflow run coworker-desktop-release.yml" in workflow
    assert '--field release_tag="$RELEASE_TAG"' in workflow
    assert "gh workflow run container-release.yml" not in workflow


def test_manual_release_publishes_the_reviewed_draft_and_container_images() -> None:
    workflow = workflow_text("release.yml")

    yaml.safe_load(workflow)
    assert 'description: "Draft release tag to publish' in workflow
    assert "Expected a release draft for" in workflow
    assert "Rebuild the draft after the latest merge" in workflow
    assert "when retrying an existing published release" in workflow
    assert "Publish a release draft from the default branch" in workflow
    assert "Candidate release $source_tag is already published" in workflow
    assert "expected 12" in workflow
    assert "coworker-release-candidate-sha:$commit_sha" in workflow
    assert 'git tag --annotate "$RELEASE_TAG"' in workflow
    assert '-f tag_name="$RELEASE_TAG"' in workflow
    assert "-F draft=false" in workflow
    assert "Remove candidate tag" in workflow
    assert '--method DELETE "repos/$GITHUB_REPOSITORY/git/refs/tags/$CANDIDATE_TAG"' in workflow
    assert "Open changelog finalization pull request" in workflow
    assert "python scripts/finalize_changelog.py" in workflow
    assert "RELEASE_PR_TOKEN" in workflow
    assert "pull-requests: write" in workflow
    assert "gh pr create" in workflow
    assert 'release_date="$(date -u +%F)"' in workflow
    assert '--body "$pr_body"' in workflow
    assert "gh workflow run container-release.yml" in workflow
    assert "gh workflow run coworker-desktop-release.yml" not in workflow
    assert '--ref "$RELEASE_TAG"' in workflow
    assert workflow.index("-F draft=false") < workflow.index(
        "gh workflow run container-release.yml"
    )


def test_desktop_candidate_build_creates_or_refreshes_a_release_draft() -> None:
    workflow = workflow_text("coworker-desktop-release.yml")

    yaml.safe_load(workflow)
    assert "github.ref_type == 'tag' || inputs.release_tag != ''" in workflow
    assert "already exists and is immutable" in workflow
    assert 'upload_tag="release-candidate-$RELEASE_TAG"' in workflow
    assert "git/refs/tags/$upload_tag" in workflow
    assert "-F force=true" in workflow
    assert 'releases/generate-notes"' in workflow
    assert '-f target_commitish="$release_sha"' in workflow
    assert "-F draft=true" in workflow
    assert 'gh release upload "$UPLOAD_TAG"' in workflow
    assert "--clobber" in workflow
    assert "Mark release candidate ready" in workflow
    assert "coworker-release-candidate-sha:$RELEASE_SHA" in workflow
