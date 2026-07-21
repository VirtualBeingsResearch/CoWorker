from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from coworker.core.config import (
    AdminConfig,
    AgentConfig,
    APIConfig,
    DesktopUpdatesConfig,
    LLMConfig,
    MemoryConfig,
    WeComConfig,
)

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
LINK_PATTERN = re.compile(r"\[[^]]*\]\(([^)]+)\)|href=[\"']([^\"']+)[\"']")
CONFIG_DEFAULT_PATTERN = re.compile(
    r"^\| `(?P<prefix>[A-Z_]+)__(?P<field>[A-Z0-9_]+)` \| `(?P<default>[^`]+)` \|",
    re.MULTILINE,
)
CONFIG_TYPES = {
    "ADMIN": AdminConfig,
    "AGENT": AgentConfig,
    "API": APIConfig,
    "DESKTOP_UPDATES": DesktopUpdatesConfig,
    "LLM": LLMConfig,
    "MEMORY": MemoryConfig,
    "WECOM": WeComConfig,
}


def test_docs_have_paired_language_versions() -> None:
    chinese_docs = sorted(
        path for path in DOCS.rglob("*.md") if not path.name.endswith(".en.md")
    )
    english_docs = sorted(DOCS.rglob("*.en.md"))

    expected_english = {path.with_name(f"{path.stem}.en.md") for path in chinese_docs}
    assert set(english_docs) == expected_english

    for chinese in chinese_docs:
        english = chinese.with_name(f"{chinese.stem}.en.md")
        assert f"]({english.name})" in chinese.read_text(encoding="utf-8")
        assert f"]({chinese.name})" in english.read_text(encoding="utf-8")


def test_local_documentation_links_resolve() -> None:
    markdown_files = [
        ROOT / "README.md",
        ROOT / "README.en.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "CONTRIBUTING.zh-CN.md",
        ROOT / "SECURITY.md",
        ROOT / "SECURITY.zh-CN.md",
        *DOCS.rglob("*.md"),
    ]

    broken: list[str] = []
    for document in markdown_files:
        for match in LINK_PATTERN.finditer(document.read_text(encoding="utf-8")):
            raw_target = next(group for group in match.groups() if group).strip().strip("<>")
            parsed = urlsplit(raw_target)
            if parsed.scheme or raw_target.startswith("#"):
                continue
            target = (document.parent / unquote(parsed.path)).resolve()
            if not target.exists():
                broken.append(f"{document.relative_to(ROOT)} -> {raw_target}")

    assert not broken, "Broken local documentation links:\n" + "\n".join(broken)


def test_documented_configuration_defaults_match_code() -> None:
    for document in (
        DOCS / "operations" / "configuration.md",
        DOCS / "operations" / "configuration.en.md",
    ):
        seen: set[str] = set()
        for match in CONFIG_DEFAULT_PATTERN.finditer(document.read_text(encoding="utf-8")):
            prefix = match.group("prefix")
            field_name = match.group("field").lower()
            env_name = f"{prefix}__{match.group('field')}"
            assert env_name not in seen, f"Duplicate setting in {document.name}: {env_name}"
            seen.add(env_name)

            config_type = CONFIG_TYPES[prefix]
            assert field_name in config_type.model_fields, (
                f"Unknown setting in {document.name}: {env_name}"
            )
            default = config_type.model_fields[field_name].get_default(
                call_default_factory=True
            )
            actual = match.group("default")
            if isinstance(default, bool):
                expected = str(default).lower()
            elif isinstance(default, float):
                assert float(actual) == default, (
                    f"Stale default in {document.name}: {env_name} should be {default}"
                )
                continue
            elif isinstance(default, list):
                expected = json.dumps(default)
            else:
                expected = str(default)
            assert actual == expected, (
                f"Stale default in {document.name}: {env_name} should be {expected}"
            )
