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


def test_channel_progress_config_labels_have_english_translations() -> None:
    admin_app = (REPOSITORY_ROOT / "web/src/admin/AdminApp.tsx").read_text(encoding="utf-8")
    i18n = (REPOSITORY_ROOT / "web/src/i18n/admin.tsx").read_text(encoding="utf-8")
    presentation = (
        REPOSITORY_ROOT / "web/src/admin/settings/configFieldPresentation.ts"
    ).read_text(encoding="utf-8")
    assert "'agent.channel_progress_enabled': '信道处理提示'" in admin_app
    assert "'agent.channel_progress_reply_reminder_seconds': '处理提示催促秒数'" in admin_app
    assert "'agent.channel_progress_timeout_seconds': '处理提示超时秒数'" in admin_app
    for phrase in (
        "信道处理提示",
        "处理提示催促秒数",
        "处理提示超时秒数",
        "开启后，企业微信与 Telegram 的私聊会立即显示处理中提示，正式回复覆盖同一条；群聊不显示。",
        "超过该秒数仍未 communicate 时向模型注入催促；0 表示只显示处理提示、不催促。",
        "超过该秒数仍未回复时，处理中提示会被换成一句中性说明并结束，不会再一直停在「正在思考中…」；0 表示一直保留到你覆盖。建议大于催促秒数。",
    ):
        assert f"'{phrase}':" in i18n
        if "communicate" in phrase or "Telegram" in phrase:
            assert phrase in presentation

