from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")
PR_SUBJECT_RE = re.compile(r"^Pull request #\d+:\s*(.+)$", re.IGNORECASE)
RELEASE_SUBJECT_RE = re.compile(
    r"^(?:(?:build|chore|ci|docs)(?:\([^)]+\))?:\s*)?"
    r"(?:bump(?: product)? version|prepare release|release|update changelog)\b",
    re.IGNORECASE,
)


def replace_text(path: Path, pattern: str, repl: str) -> None:
    text = path.read_text(encoding="utf-8")
    next_text = re.sub(pattern, repl, text, count=1, flags=re.MULTILINE)
    if next_text == text:
        raise SystemExit(f"no version field matched in {path.relative_to(ROOT)}")
    path.write_text(next_text, encoding="utf-8")


def update_package_json(path: Path, version: str) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = version
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def update_package_lock(path: Path, version: str) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = version
    root_package = data.get("packages", {}).get("")
    if isinstance(root_package, dict):
        root_package["version"] = version
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def update_uv_lock(path: Path, version: str) -> None:
    replace_text(
        path,
        r'(^\[\[package\]\]\nname = "coworker"\nversion = )"[^"]+"',
        rf'\g<1>"{version}"',
    )


def update_cargo_lock(path: Path, version: str) -> None:
    for package in ("coworker-desktop-app", "coworker-desktop-core"):
        replace_text(
            path,
            rf'(^\[\[package\]\]\nname = "{package}"\nversion = )"[^"]+"',
            rf'\g<1>"{version}"',
        )


def git_output(root: Path, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def changelog_range(root: Path) -> str | None:
    latest_tag = git_output(
        root,
        [
            "describe",
            "--tags",
            "--match",
            "v[0-9]*",
            "--match",
            "coworker-desktop-v[0-9]*",
            "--abbrev=0",
        ],
    )
    if latest_tag:
        return f"{latest_tag}..HEAD"

    latest_version_commit = git_output(root, ["log", "-1", "--format=%H", "--", "VERSION"])
    if latest_version_commit:
        return f"{latest_version_commit}..HEAD"

    return None


def normalize_changelog_subject(subject: str) -> str | None:
    subject = re.sub(r"\s+", " ", subject.strip())
    pr_match = PR_SUBJECT_RE.match(subject)
    if pr_match:
        subject = pr_match.group(1).strip()
    if (
        not subject
        or subject.startswith("Merge ")
        or RELEASE_SUBJECT_RE.match(subject)
    ):
        return None
    return subject


def collect_changelog_items(root: Path) -> list[str]:
    args = ["log", "--reverse", "--no-merges", "--format=%s"]
    commit_range = changelog_range(root)
    if commit_range:
        args.append(commit_range)

    output = git_output(root, args)
    if not output:
        return []

    items: list[str] = []
    seen: set[str] = set()
    for line in output.splitlines():
        item = normalize_changelog_subject(line)
        if item and item not in seen:
            items.append(item)
            seen.add(item)
    return items


def generated_changelog_body(root: Path) -> str:
    items = collect_changelog_items(root)
    if not items:
        return "- TODO"
    return "\n".join(f"- {item}" for item in items)


def upsert_changelog_section(text: str, version: str, body: str) -> str:
    heading = f"## {version} - Unreleased"
    section = f"{heading}\n\n{body}\n\n"
    heading_re = re.compile(rf"^##\s+{re.escape(version)}\s+-\s+Unreleased\s*$", re.MULTILINE)
    match = heading_re.search(text)

    if match:
        next_match = re.search(r"^##\s+", text[match.end() :], re.MULTILINE)
        section_end = match.end() + next_match.start() if next_match else len(text)
        existing_body = text[match.end() : section_end].strip()
        if existing_body and existing_body != "- TODO":
            return text
        return (text[: match.start()] + section + text[section_end:].lstrip("\n")).rstrip() + "\n"

    unreleased_re = re.compile(r"^##\s+Unreleased\s*$", re.MULTILINE | re.IGNORECASE)
    unreleased_match = unreleased_re.search(text)
    if unreleased_match:
        next_match = re.search(r"^##\s+", text[unreleased_match.end() :], re.MULTILINE)
        section_end = (
            unreleased_match.end() + next_match.start() if next_match else len(text)
        )
        unreleased_body = text[unreleased_match.end() : section_end].strip()
        release_body = (
            unreleased_body if unreleased_body and unreleased_body != "- TODO" else body
        )
        replacement = f"## Unreleased\n\n## {version} - Unreleased\n\n{release_body}\n\n"
        return (
            text[: unreleased_match.start()]
            + replacement
            + text[section_end:].lstrip("\n")
        ).rstrip() + "\n"

    if text.startswith("# Changelog"):
        title_end = text.find("\n")
        if title_end == -1:
            return f"# Changelog\n\n{section}".rstrip() + "\n"
        title = text[: title_end + 1]
        rest = text[title_end + 1 :].lstrip("\n")
        return (title + "\n" + section + rest).rstrip() + "\n"

    return (f"# Changelog\n\n{section}" + text.lstrip("\n")).rstrip() + "\n"


def finalize_changelog_section(text: str, version: str, released_on: str) -> str:
    heading_re = re.compile(
        rf"^(##[ \t]+{re.escape(version)}[ \t]+-[ \t]+)Unreleased[ \t]*$",
        re.MULTILINE | re.IGNORECASE,
    )
    finalized = heading_re.sub(rf"\g<1>{released_on}", text, count=1)
    if finalized != text:
        return finalized

    finalized_heading_re = re.compile(
        rf"^##[ \t]+{re.escape(version)}[ \t]+-[ \t]+{re.escape(released_on)}[ \t]*$",
        re.MULTILINE,
    )
    if finalized_heading_re.search(text):
        return text

    raise ValueError(f"no unreleased changelog section found for {version}")


def ensure_changelog(version: str, root: Path = ROOT) -> None:
    path = root / "CHANGELOG.md"
    if not path.exists():
        path.write_text("# Changelog\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    path.write_text(
        upsert_changelog_section(text, version, generated_changelog_body(root)),
        encoding="utf-8",
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not SEMVER_RE.match(argv[1]):
        print("usage: python scripts/bump_version.py <semver>", file=sys.stderr)
        return 2
    version = argv[1]

    (ROOT / "VERSION").write_text(version + "\n", encoding="utf-8")
    replace_text(ROOT / "pyproject.toml", r'^version = "[^"]+"', f'version = "{version}"')
    update_uv_lock(ROOT / "uv.lock", version)
    replace_text(ROOT / "Cargo.toml", r'^version = "[^"]+"', f'version = "{version}"')
    update_cargo_lock(ROOT / "Cargo.lock", version)
    replace_text(
        ROOT / "apps/coworker-desktop/desktop/src-tauri/tauri.conf.json",
        r'("version": )"[^"]+"',
        rf'\1"{version}"',
    )
    update_package_json(ROOT / "apps/coworker-desktop/desktop/package.json", version)
    update_package_lock(ROOT / "apps/coworker-desktop/desktop/package-lock.json", version)
    update_package_json(ROOT / "web/package.json", version)
    update_package_lock(ROOT / "web/package-lock.json", version)
    ensure_changelog(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
