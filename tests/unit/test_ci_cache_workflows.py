from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def workflow_text(name: str) -> str:
    return (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")


def test_main_warmer_populates_every_ci_dependency_cache() -> None:
    workflow = workflow_text("warm-ci-caches.yml")

    yaml.safe_load(workflow)
    assert "name: Warm CI caches" in workflow
    assert "schedule:" not in workflow
    assert "cancel-in-progress: false" in workflow
    assert 'python: "3.13"' in workflow
    assert 'python: "3.14"' in workflow
    assert "npm ci" in workflow
    assert "cargo test --workspace --locked --no-run" in workflow
    assert "go test -race -run '^$' ./..." in workflow
    assert "docker/build-push-action@v7" in workflow
    assert "cache-to: type=gha,mode=max,scope=coworker-relay" in workflow
    assert "aquasecurity/trivy-action@v0.36.0" in workflow


def test_merge_queue_restores_but_does_not_save_large_caches() -> None:
    workflow = workflow_text("ci.yml")

    yaml.safe_load(workflow)
    assert "actions/cache/restore@v6" in workflow
    assert "actions/cache/save@v6" in workflow
    assert "save-cache: ${{ github.event_name != 'merge_group' }}" in workflow
    assert "save-if: ${{ github.event_name != 'merge_group' }}" in workflow
    assert "cache-from: type=gha,scope=coworker-relay" in workflow
    assert (
        "cache-to: ${{ github.event_name != 'merge_group' && "
        "'type=gha,mode=max,scope=coworker-relay' || '' }}" in workflow
    )
