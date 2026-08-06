import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_workspace_labels_are_categories_not_page_names() -> None:
    source = (REPOSITORY_ROOT / "web/src/admin/AdminApp.tsx").read_text(encoding="utf-8")
    nav_source = source.split("const NAV:", 1)[1].split("const WORKSPACES:", 1)[0]
    workspace_source = source.split("const WORKSPACES:", 1)[1].split(
        "const DEFAULT_SECTION_BY_WORKSPACE:", 1
    )[0]

    page_labels = set(re.findall(r"label: '([^']+)'", nav_source))
    workspace_labels = re.findall(r"label: '([^']+)'", workspace_source)

    assert workspace_labels == ["观测", "运维", "配置", "关系", "扩展"]
    assert page_labels.isdisjoint(workspace_labels)


def test_workspace_labels_have_english_translations() -> None:
    source = (REPOSITORY_ROOT / "web/src/i18n/admin.tsx").read_text(encoding="utf-8")

    for chinese, english in (
        ("观测", "Observability"),
        ("运维", "Ops"),
        ("配置", "Config"),
        ("关系", "Relationships"),
        ("扩展", "Extensions"),
    ):
        assert f"'{chinese}': '{english}'" in source
