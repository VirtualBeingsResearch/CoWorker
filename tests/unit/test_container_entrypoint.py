from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "scripts" / "container-entrypoint.sh"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _create_repository(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Container Test")
    _git(repository, "config", "user.email", "container-test@example.com")
    (repository / "app.txt").write_text("bundled source\n", encoding="utf-8")
    _git(repository, "add", "app.txt")
    _git(repository, "commit", "-m", "initial")

    bundle = tmp_path / "repository.bundle"
    _git(repository, "bundle", "create", str(bundle), "--all")
    return repository, bundle


def _run_entrypoint(
    workspace: Path,
    state: Path,
    bundle: Path,
    output: Path,
    image_revision_file: Path | None = None,
    image_branch_file: Path | None = None,
    embedded_bundle: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "COWORKER_WORKSPACE_PATH": str(workspace),
        "COWORKER_STATE_PATH": str(state),
        "COWORKER_TEST_OUTPUT": str(output),
    }
    if embedded_bundle:
        env["COWORKER_BUNDLED_REPOSITORY_PATH"] = str(bundle)
    else:
        env["COWORKER_REPOSITORY_BUNDLE"] = str(bundle)
    if image_revision_file is not None:
        env["COWORKER_IMAGE_REVISION_FILE"] = str(image_revision_file)
    if image_branch_file is not None:
        env["COWORKER_IMAGE_BRANCH_FILE"] = str(image_branch_file)
    return subprocess.run(
        [
            str(ENTRYPOINT),
            "/bin/sh",
            "-c",
            'printf "%s" "$PWD" > "$COWORKER_TEST_OUTPUT"',
        ],
        env=env,
        capture_output=True,
        text=True,
    )


def test_image_workspace_is_reset_to_bundled_source(
    tmp_path: Path,
) -> None:
    _, bundle = _create_repository(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image_revision_file = tmp_path / "repository.revision"
    image_revision_file.write_text("trusted-image-revision\n", encoding="utf-8")
    (workspace / ".coworker-image-workspace").write_text(
        image_revision_file.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (workspace / "app.txt").write_text("image source\n", encoding="utf-8")
    (workspace / "stale.txt").write_text("stale image file\n", encoding="utf-8")
    state = tmp_path / "state"
    output = tmp_path / "cwd.txt"

    result = _run_entrypoint(
        workspace,
        state,
        bundle,
        output,
        image_revision_file=image_revision_file,
    )

    assert result.returncode == 0, result.stderr
    assert (workspace / ".git").is_dir()
    assert not (workspace / ".coworker-image-workspace").exists()
    assert (workspace / "app.txt").read_text(encoding="utf-8") == "bundled source\n"
    assert not (workspace / "stale.txt").exists()
    assert _git(workspace, "branch", "--show-current") == "main"
    assert _git(workspace, "config", "--bool", "--get", "coworker.containerManaged") == "true"
    assert _git(workspace, "status", "--short", "--", "app.txt") == ""
    assert (workspace / "data").resolve() == state.resolve()
    assert (workspace / ".coworker" / "skills").is_dir()
    assert output.read_text(encoding="utf-8") == str(workspace)


def test_existing_bind_mounted_repository_is_used_in_place(tmp_path: Path) -> None:
    repository, bundle = _create_repository(tmp_path)
    workspace = tmp_path / "workspace"
    _git(tmp_path, "clone", str(repository), str(workspace))
    state = tmp_path / "state"
    output = tmp_path / "cwd.txt"

    result = _run_entrypoint(workspace, state, bundle, output)

    assert result.returncode == 0, result.stderr
    assert "Using existing Git workspace" in result.stdout
    assert output.read_text(encoding="utf-8") == str(workspace)
    assert (workspace / "data").resolve() == state.resolve()


def test_clean_managed_workspace_fast_forwards_to_new_image_revision(
    tmp_path: Path,
) -> None:
    repository, _ = _create_repository(tmp_path)
    original_revision = _git(repository, "rev-parse", "HEAD")
    (repository / "app.txt").write_text("updated source\n", encoding="utf-8")
    _git(repository, "add", "app.txt")
    _git(repository, "commit", "-m", "update")
    image_revision = _git(repository, "rev-parse", "HEAD")
    image_bundle = tmp_path / "updated.bundle"
    _git(repository, "bundle", "create", str(image_bundle), "--all")

    workspace = tmp_path / "workspace"
    _git(tmp_path, "clone", str(repository), str(workspace))
    _git(workspace, "reset", "--hard", original_revision)
    _git(workspace, "config", "--local", "coworker.containerManaged", "true")
    image_revision_file = tmp_path / "repository.revision"
    image_revision_file.write_text(f"{image_revision}\n", encoding="utf-8")
    image_branch_file = tmp_path / "repository.branch"
    image_branch_file.write_text("main\n", encoding="utf-8")

    result = _run_entrypoint(
        workspace,
        tmp_path / "state",
        image_bundle,
        tmp_path / "cwd.txt",
        image_revision_file=image_revision_file,
        image_branch_file=image_branch_file,
        embedded_bundle=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Fast-forwarding managed workspace" in result.stdout
    assert _git(workspace, "rev-parse", "HEAD") == image_revision
    assert (workspace / "app.txt").read_text(encoding="utf-8") == "updated source\n"


def test_modified_managed_workspace_is_not_fast_forwarded(tmp_path: Path) -> None:
    repository, _ = _create_repository(tmp_path)
    original_revision = _git(repository, "rev-parse", "HEAD")
    (repository / "app.txt").write_text("updated source\n", encoding="utf-8")
    _git(repository, "add", "app.txt")
    _git(repository, "commit", "-m", "update")
    image_revision = _git(repository, "rev-parse", "HEAD")
    image_bundle = tmp_path / "updated.bundle"
    _git(repository, "bundle", "create", str(image_bundle), "--all")

    workspace = tmp_path / "workspace"
    _git(tmp_path, "clone", str(repository), str(workspace))
    _git(workspace, "reset", "--hard", original_revision)
    _git(workspace, "config", "--local", "coworker.containerManaged", "true")
    (workspace / "app.txt").write_text("local source\n", encoding="utf-8")
    image_revision_file = tmp_path / "repository.revision"
    image_revision_file.write_text(f"{image_revision}\n", encoding="utf-8")
    image_branch_file = tmp_path / "repository.branch"
    image_branch_file.write_text("main\n", encoding="utf-8")

    result = _run_entrypoint(
        workspace,
        tmp_path / "state",
        image_bundle,
        tmp_path / "cwd.txt",
        image_revision_file=image_revision_file,
        image_branch_file=image_branch_file,
        embedded_bundle=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Keeping locally modified workspace" in result.stdout
    assert _git(workspace, "rev-parse", "HEAD") == original_revision
    assert (workspace / "app.txt").read_text(encoding="utf-8") == "local source\n"


def test_nonempty_non_repository_workspace_is_not_overwritten(tmp_path: Path) -> None:
    _, bundle = _create_repository(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = workspace / "keep.txt"
    sentinel.write_text("keep\n", encoding="utf-8")

    result = _run_entrypoint(
        workspace,
        tmp_path / "state",
        bundle,
        tmp_path / "cwd.txt",
    )

    assert result.returncode != 0
    assert "Refusing to initialize a non-empty workspace without .git" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
