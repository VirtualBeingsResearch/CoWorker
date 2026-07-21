from __future__ import annotations

import asyncio
import re
from importlib import resources
from pathlib import Path

import pytest

from coworker.agent.bubble import Bubble, BubbleStore
from coworker.agent.incoming_content import format_event_text
from coworker.agent.subconscious_mode import SubconsciousModeLoader
from coworker.channels.wecom import adapter as wecom_adapter
from coworker.core.config import I18NConfig
from coworker.core.types import IncomingEvent
from coworker.i18n import (
    SupportedLocale,
    bind_locale,
    browser_locale,
    locale_context,
    normalize_locale,
    tr,
    validate_catalogs,
)
from coworker.i18n.resources import (
    companion_candidates,
    load_markdown_companion,
    resolve_localized_path,
)
from coworker.i18n.runtime import catalog
from coworker.tools.base import ToolDefinition
from coworker.tools.browser_tools import BrowserOpenTool, BrowserSessionStore
from coworker.tools.bubble_tools import BubbleCheckTool
from coworker.tools.reasoning_tools import infer_task_kind


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("zh", SupportedLocale.ZH_CN),
        ("zh_CN", SupportedLocale.ZH_CN),
        ("zh-Hans", SupportedLocale.ZH_CN),
        ("en", SupportedLocale.EN),
        ("en-US", SupportedLocale.EN),
        ("en_GB", SupportedLocale.EN),
    ],
)
def test_locale_aliases_are_normalized(value: str, expected: SupportedLocale) -> None:
    assert normalize_locale(value) is expected
    assert I18NConfig(locale=value).locale is expected


def test_unknown_locale_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported locale"):
        normalize_locale("fr-FR")
    with pytest.raises(ValueError):
        I18NConfig(locale="fr-FR")


def test_catalogs_have_strict_key_and_placeholder_parity() -> None:
    validate_catalogs()
    assert catalog("zh-CN").keys() == catalog("en").keys()


def test_catalog_files_are_package_resources() -> None:
    root = resources.files("coworker.i18n.catalogs")
    for locale in SupportedLocale:
        names = {entry.name for entry in root.joinpath(locale.value).iterdir()}
        assert {"common.toml", "prompt.toml", "runtime.toml"} <= names


def test_missing_translation_never_returns_the_key() -> None:
    with pytest.raises(KeyError, match="Missing i18n catalog entry"):
        tr("this.key.must.not.leak")


@pytest.mark.asyncio
async def test_locale_context_is_isolated_between_async_tasks() -> None:
    async def render(locale: str) -> tuple[str, str]:
        with locale_context(locale):
            await asyncio.sleep(0)
            return tr("calendar.monday"), browser_locale()

    zh, en = await asyncio.gather(render("zh-CN"), render("en"))
    assert zh == ("周一", "zh-CN")
    assert en == ("Monday", "en-US")


@pytest.mark.asyncio
async def test_background_locale_is_fixed_when_work_is_created() -> None:
    async def render() -> str:
        await asyncio.sleep(0)
        return tr("calendar.monday")

    with locale_context("en"):
        bound = bind_locale(render)
    with locale_context("zh-CN"):
        rendered = await asyncio.create_task(bound)
    assert rendered == "Monday"


def test_incoming_wrapper_changes_language_but_preserves_user_content() -> None:
    event = IncomingEvent(
        participant_id="alice",
        conversation_id="c-1",
        source="websocket",
        content="这段用户内容不能被翻译。",
    )
    with locale_context("en"):
        rendered = format_event_text(event)
    assert "from WebSocket" in rendered
    assert "这段用户内容不能被翻译。" in rendered


@pytest.mark.asyncio
async def test_english_bubble_status_preserves_goal_content() -> None:
    store = BubbleStore()
    bubble = store.create(
        goal="保留中文用户目标", forked_context=[], max_cycles=4
    )
    assert isinstance(bubble, Bubble)
    with locale_context("en"):
        result = await BubbleCheckTool(store).execute(bubble.id)
    assert "Status: running" in result.content
    assert "Current cycle: 0/4" in result.content
    assert "保留中文用户目标" in result.content


def test_english_wecom_wrappers_preserve_quoted_source_text() -> None:
    frame = {
        "body": {
            "chattype": "group",
            "from": {"userid": "U-alice"},
        }
    }
    quote = {
        "msgquote": {
            "msgtype": "text",
            "from_userid": "U-alice",
            "text": {"content": "这段第三方原文保持不变"},
        }
    }
    with locale_context("en"):
        sender = wecom_adapter._sender_prefix(frame)
        rendered_quote = wecom_adapter._quote_prefix(quote)
    assert sender == "[sender userid=U-alice]\n"
    assert "quoting U-alice" in rendered_quote
    assert "这段第三方原文保持不变" in rendered_quote


def test_companion_lookup_prefers_exact_then_base_then_original(tmp_path: Path) -> None:
    original = tmp_path / "SKILL.md"
    base = tmp_path / "SKILL.zh.md"
    exact = tmp_path / "SKILL.zh-CN.md"
    original.write_text("base", encoding="utf-8")
    base.write_text("language", encoding="utf-8")
    exact.write_text("exact", encoding="utf-8")

    assert companion_candidates(original, "zh-CN") == (exact, base, original)
    assert resolve_localized_path(original, "zh-CN") == exact
    exact.unlink()
    assert resolve_localized_path(original, "zh-CN") == base
    base.unlink()
    assert resolve_localized_path(original, "zh-CN") == original


def test_invalid_companion_warns_and_falls_back_without_changing_metadata(
    tmp_path: Path,
) -> None:
    original = tmp_path / "MODE.md"
    original.write_text("unused", encoding="utf-8")
    (tmp_path / "MODE.en.md").write_text(
        "---\nname: changed\ngoal: English {different}\n---\nBody {different}\n",
        encoding="utf-8",
    )
    with locale_context("en"):
        loaded = load_markdown_companion(
            original,
            base_fields={"name": "stable", "goal": "中文 {goal}"},
            base_body="正文 {goal}",
            localizable_fields=("goal",),
        )
    assert loaded.fields["goal"] == "中文 {goal}"
    assert loaded.body == "正文 {goal}"
    assert loaded.warning and "MODE.en.md" in loaded.warning


def test_builtin_subconscious_modes_have_valid_english_companions() -> None:
    with locale_context("en"):
        loader = SubconsciousModeLoader(".coworker/subconscious")
        loader.load_all()
    assert not loader.consume_load_warnings()
    assert set(loader.list_names()) >= {
        "audit",
        "explore",
        "garden",
        "introspect",
        "meta",
        "summarize",
    }
    introspect = loader.get("introspect")
    assert introspect is not None
    assert introspect.trigger == "periodic"
    assert "Assess" in introspect.goal
    assert "{bubble_id}" in introspect.body


def test_all_tool_names_have_english_catalog_descriptions() -> None:
    source_root = Path("src/coworker/tools")
    names: set[str] = set()
    for source in source_root.glob("*.py"):
        names.update(re.findall(r'name="([a-zA-Z0-9_]+)"', source.read_text(encoding="utf-8")))
    english = catalog("en")
    missing = sorted(name for name in names if f"tool_schema.description.{name}" not in english)
    assert not missing


def test_english_tool_schema_localizes_descriptions_without_mutating_protocol() -> None:
    definition = ToolDefinition(
        name="read_file",
        description="读取文件",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "mode": {"type": "string", "enum": ["raw", "text"]},
            },
            "required": ["path"],
        },
    )
    with locale_context("en"):
        schema = definition.to_schema()
    assert not re.search(r"[\u3400-\u9fff]", str(schema))
    assert schema["name"] == "read_file"
    assert schema["parameters"]["properties"]["mode"]["enum"] == ["raw", "text"]
    assert definition.description == "读取文件"


def test_browser_schema_default_follows_runtime_locale() -> None:
    with locale_context("en"):
        definition = BrowserOpenTool(BrowserSessionStore()).definition
    assert definition.parameters["properties"]["locale"]["default"] == "en-US"


@pytest.mark.parametrize(
    ("description", "kind"),
    [
        ("[growth] improve", "growth"),
        ("[maintenance] clean", "maintenance"),
        ("[subconscious] tune", "subconscious"),
        ("[成长] 提升", "growth"),
        ("[维护] 清理", "maintenance"),
        ("[潜意识] 调整", "subconscious"),
        ("ordinary user task", ""),
    ],
)
def test_task_kind_uses_stable_markers_and_legacy_inference(description: str, kind: str) -> None:
    assert infer_task_kind(description) == kind
