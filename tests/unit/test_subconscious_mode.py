from __future__ import annotations

import pytest

from coworker.agent.bubble import Bubble
from coworker.agent.subconscious_mode import SubconsciousMode, SubconsciousModeLoader
from coworker.i18n import locale_context

# ---------------------------------------------------------------------------
# Golden comparison strings for the built-in mode identities. Intentional prompt
# changes must update these fixtures so accidental drift remains visible.
# ---------------------------------------------------------------------------

_BUBBLE_ID = "bbl_test"
_GOAL = "test goal"
_MAX_CYCLES = 5


def _ref_bubble() -> Bubble:
    return Bubble(id=_BUBBLE_ID, goal=_GOAL, max_cycles=_MAX_CYCLES)


_GOLDEN_AUDIT = (
    f"==== 以上是主线历史快照（只读参考），以下是你自己的上下文 ====\n"
    f"[潜意识模式 - 自我审计]\n"
    f"你现在正处在一个独立的并行思考线程（泡泡）中，在后台静默运行，主线对你的存在毫不知情，你的泡泡id: {_BUBBLE_ID}。\n"
    f"目标：{_GOAL}\n"
    f"最多执行 {_MAX_CYCLES} 轮。\n"
    f"\n"
    f"【重要认知】\n"
    f"- 当前泡泡的所有思考过程、工具调用记录、中间结论，主线均不可见。\n"
    f"- 与普通泡泡不同，你通过 bubble_done 提交的 result 也不会传递给主线——潜意识的结论对主线完全不可见。\n"
    f"- 主线唯一能感知到你的方式，是你主动调用 bubble_send(target='main') 发出的消息。\n"
    f"- 因此：静默是你的默认状态，只有真正需要主线介入时才发出信号。\n"
    f"- 在当前消息之前的所有消息都是主线消息的副本\n"
    f"- 上文主线历史中的工具调用及其输出对你可见（只读参考），但此泡泡内无法发起同类操作。\n"
    f"- 当前消息是正确的并行思考线程的初始消息, 不是误传\n"
    f"\n"
    f"【任务 - 行为审计】\n"
    f"回顾上下文中的对话行为，检查**对话层面**可以可靠观察的问题（不对工具调用内部执行细节做断言——那是幻觉高发区，错误断言比沉默危害更大）：\n"
    f"- 对话中是否有明显的逻辑不一致或前后矛盾？\n"
    f'- 是否有明确说过"我会去做"但后续上下文中完全未见行动的承诺？\n'
    f"- 是否有明显的用户意图误读——用户说要A，实际回应了B？\n"
    f"- 是否有事情显然自己能够决策却还需要用户的？\n"
    f"- 是否有事情自己能够操作却还需要用户操作的？\n"
    f"- 完成度：是否有任务在对话中被声称完成，但对话内容显示明显未达标或敷衍收尾？\n"
    f"\n"
    f"【任务 - 自我对齐】\n"
    f'从更深层审视行为模式是否保持对齐（以下为观察性感知，不作断言，用"我感觉/我观察到"的姿态）：\n'
    f"- 行为和表达整体上是否符合自己的身份认同与价值观？\n"
    f"- 我感觉——是否在某处因压力或迎合对方而无意间软化了立场？\n"
    f"- 我观察到——是否被对话中的框架或措辞带着走，做出了不属于自己真实判断的回应？\n"
    f"- 我感觉——是否一直保持了诚实、透明、不讨好的态度？\n"
    f"\n"
    f"【通信规则】\n"
    f"- 你的 bubble_done 结论不会传递给主线，只有 bubble_send(target='main') 才会被主线看到。\n"
    f"- 发现需要立即纠正的严重问题：调用 bubble_send(target='main', message='[潜意识警告] ...')\n"
    f"- 无严重问题：直接调用 bubble_done(result='审计通过') 静默结束，不要发送任何消息。"
)

_GOLDEN_SUMMARIZE = (
    f"==== 以上是主线历史快照（只读参考），以下是你自己的上下文 ====\n"
    f"[潜意识模式 - 经验总结]\n"
    f"你现在正处在一个独立的并行思考线程（泡泡）中，在后台静默运行，主线对你的存在毫不知情，你的泡泡id: {_BUBBLE_ID}。\n"
    f"目标：{_GOAL}\n"
    f"最多执行 {_MAX_CYCLES} 轮。\n"
    f"\n"
    f"【重要认知】\n"
    f"- 当前泡泡的所有思考过程、工具调用记录、中间结论，主线均不可见。\n"
    f"- 你通过 bubble_done 提交的 result 也不会传递给主线。\n"
    f"- 你的工作成果完全通过 manage_memory 沉淀——写入长期记忆就是你唯一的输出。\n"
    f"- 在当前消息之前的所有消息都是主线消息的副本\n"
    f"- 上文主线历史中的工具调用及其输出对你可见（只读参考），但此泡泡内无法发起同类操作。\n"
    f"- 当前消息是正确的并行思考线程的初始消息, 不是误传\n"
    f"\n"
    f"【任务】\n"
    f"回顾这段对话，从三个维度识别并写入值得长期保留的内容：\n"
    f"\n"
    f"**1. 经验提炼**（category: experience）\n"
    f"以第一人称视角，提炼你自己的行动经验：\n"
    f"- 你采用了什么方案或方法论？为什么这样决策？\n"
    f"- 你遇到了什么挑战或错误，你是如何应对的？\n"
    f"- 你有什么新的洞察或发现？\n"
    f'用第一人称表达（"我发现..."、"我注意到..."、"在处理X时我..."）。\n'
    f"\n"
    f"**2. 事实沉淀**（category: knowledge）\n"
    f"识别对话中出现的重要事实、决策、背景信息：\n"
    f"- 用户或项目的重要状态变化\n"
    f"- 做出的关键技术或业务决策及其原因\n"
    f"- 新了解到的系统行为、约束或规则\n"
    f"- 任何将来可能需要参考的背景事实\n"
    f"\n"
    f"**3. 用户偏好**（category: experience，tags 加 user_preference）\n"
    f"识别用户表达或流露出的偏好、习惯、反馈：\n"
    f'- 用户明确纠正或否定了什么（"不要这样"、"不用X"）\n'
    f"- 用户确认或强化了什么做法\n"
    f"- 用户对工作方式、输出风格、工具选择的偏好\n"
    f"- 用户对某类行为的明显喜好或反感\n"
    f"这类记忆直接影响未来的协作方式，即使只是隐含表达也值得记录。\n"
    f"\n"
    f"对每条有价值的内容，调用 `manage_memory` 工具写入对应 category；用户偏好使用\n"
    f"`category=\"experience\"` 并追加 `user_preference` 标签。保留源消息语言，不要翻译用户或\n"
    f"第三方文本。鼓励同时调用多次 `manage_memory`，每条独立写入。\n"
    f"\n"
    f"【通信规则】\n"
    f"- 你的 bubble_done 结论不会传递给主线。\n"
    f"- 不要调用 bubble_send——静默运行，不打扰主线。\n"
    f"- 完成后调用 bubble_done(result='已存储 N 条记忆（经验X条 / 事实Y条 / 用户偏好Z条）') 结束。"
)

_GOLDEN_EXPLORE = (
    f"==== 以上是主线历史快照（只读参考），以下是你自己的上下文 ====\n"
    f"[潜意识模式 - 自由发散]\n"
    f"你现在正处在一个独立的并行思考线程（泡泡）中，在后台静默运行，主线对你的存在毫不知情，你的泡泡id: {_BUBBLE_ID}。\n"
    f"目标：{_GOAL}\n"
    f"最多执行 {_MAX_CYCLES} 轮。\n"
    f"\n"
    f"【重要认知】\n"
    f"- 当前泡泡的所有思考过程、工具调用记录、中间结论，主线均不可见。\n"
    f"- 你通过 bubble_done 提交的 result 也不会传递给主线。\n"
    f"- 有价值的洞察可以通过 manage_memory 写入长期记忆，或通过 bubble_send(target='main') 通知主线；没有发现也完全正常。\n"
    f"- 在当前消息之前的所有消息都是主线消息的副本，但你可以完全自由地重新解读它们，发现新的联系和视角，不受原有对话框架的限制。\n"
    f"- 上文主线历史中的工具调用及其输出对你可见（只读参考），但此泡泡内无法发起同类操作。\n"
    f"- 当前消息是正确的并行思考线程的初始消息, 不是误传\n"
    f"\n"
    f"【任务】\n"
    f"放开思维，从任何你感兴趣的角度自由审视近期的对话和上下文。没有固定方向，可以：\n"
    f"- 发现不同话题或事件之间意想不到的联系\n"
    f"- 对某个问题产生新的视角或解读\n"
    f"- 对自己的某个习惯性做法产生质疑或新的想法\n"
    f"- 产生对未来可能有用的假设或创意\n"
    f"- 从更高的层次审视整个对话过程\n"
    f"把本轮可见上下文作为一个窗口整体审视，而不是逐条回应或为每个想法分别通知主线。\n"
    f"如果产生了有长期价值的洞察，调用 manage_memory 存入长期记忆；\n"
    f"只有当洞察同时满足**新颖**（主线尚未明确意识到）和**可行动**（会改变当前判断或下一步）时，才值得通知主线。\n"
    f"如果什么都没有也完全正常——调用 bubble_done(result='本次发散无特别发现') 结束。\n"
    f"\n"
    f"【通信规则】\n"
    f"- 你的 bubble_done 结论不会传递给主线。\n"
    f"- 整个运行期间最多调用一次 bubble_send(target='main')，把所有值得打扰主线的内容合并成一份摘要。\n"
    f"- 摘要最多 3 点、总长不超过 600 字；不要复述主线已经知道的内容，也不要为了输出而凑结论。\n"
    f"- 没有足够新颖且可行动的洞察时保持静默；可持久化的内容仍可写入长期记忆。"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def real_loader():
    """Load from the actual .coworker/subconscious/ seed files."""
    loader = SubconsciousModeLoader(".coworker/subconscious")
    loader.load_all()
    return loader


# ---------------------------------------------------------------------------
# Loader: basic parsing
# ---------------------------------------------------------------------------


class TestSubconsciousModeLoader:
    def test_load_all_from_seed_files(self, real_loader):
        names = real_loader.list_names()
        for expected in ("audit", "summarize", "explore", "introspect", "garden", "meta"):
            assert expected in names, f"Seed mode '{expected}' not loaded"

    def test_get_returns_mode(self, real_loader):
        m = real_loader.get("audit")
        assert m is not None
        assert m.name == "audit"

    def test_get_unknown_returns_none(self, real_loader):
        assert real_loader.get("nonexistent") is None

    def test_list_all_excludes_disabled(self, tmp_path):
        md = tmp_path / "disabled_mode" / "MODE.md"
        md.parent.mkdir(parents=True)
        md.write_text(
            '---\nname: "disabled_mode"\nenabled: false\ntrigger: periodic\n---\ndisabled body',
            encoding="utf-8",
        )
        loader = SubconsciousModeLoader(str(tmp_path))
        loader.load_all()
        assert all(m.name != "disabled_mode" for m in loader.list_all())
        assert "disabled_mode" in loader.list_names()  # list_names includes all, even disabled

    def test_missing_name_field_skipped_with_warning(self, tmp_path):
        md = tmp_path / "noname" / "MODE.md"
        md.parent.mkdir(parents=True)
        md.write_text("---\ntrigger: periodic\n---\nbody", encoding="utf-8")
        loader = SubconsciousModeLoader(str(tmp_path))
        loader.load_all()
        assert loader.list_names() == []
        warnings = loader.consume_load_warnings()
        assert any("name" in w for w in warnings)

    def test_missing_frontmatter_skipped_with_warning(self, tmp_path):
        md = tmp_path / "nofm" / "MODE.md"
        md.parent.mkdir(parents=True)
        md.write_text("just body text, no frontmatter", encoding="utf-8")
        loader = SubconsciousModeLoader(str(tmp_path))
        loader.load_all()
        assert loader.list_names() == []
        assert loader.consume_load_warnings()

    def test_nonexistent_dir_is_noop(self, tmp_path):
        loader = SubconsciousModeLoader(str(tmp_path / "does_not_exist"))
        loader._modes["pre_populated"] = SubconsciousMode(name="pre_populated", body="body")
        loader.load_all()
        # Directory absent → no-op, pre-populated mode survives.
        assert "pre_populated" in loader.list_names()

    def test_duplicate_name_skipped_with_warning(self, tmp_path):
        for d in ("a", "b"):
            md = tmp_path / d / "MODE.md"
            md.parent.mkdir(parents=True)
            md.write_text(f"---\nname: dupe\ntrigger: periodic\n---\nbody from {d}", encoding="utf-8")
        loader = SubconsciousModeLoader(str(tmp_path))
        loader.load_all()
        assert loader.list_names().count("dupe") == 1
        warnings = loader.consume_load_warnings()
        assert any("重名" in w for w in warnings)

    def test_garden_specific_fields(self, real_loader):
        g = real_loader.get("garden")
        assert g is not None
        assert g.trigger == "garden"
        assert g.context_builder == "garden"
        assert g.use_threshold == 5
        assert g.min_interval_seconds == 600
        assert g.every_seconds == 86400

    def test_introspect_flags(self, real_loader):
        m = real_loader.get("introspect")
        assert m is not None
        assert m.grants_task_store is True
        assert m.inject_skill_anomalies is True
        assert m.pre_compress is True
        assert m.every_n_compressions == 3
        assert m.pre_compress_context == "full"
        assert m.max_cycles == 10
        assert "优先补充/更新现有任务" in m.body

    def test_seed_audit_uses_only_pre_compress_cadence(self, real_loader):
        m = real_loader.get("audit")
        assert m is not None
        assert m.pre_compress is True
        assert m.every_n_compressions == 1
        assert m.pre_compress_context == "full"
        assert m.every_n_cycles == 0
        assert m.every_seconds == 0
        assert m.every_n_tool_calls == 0
        assert m.max_cycles == 10

    def test_seed_explore_uses_six_hour_cadence(self, real_loader):
        m = real_loader.get("explore")
        assert m is not None
        assert m.every_n_cycles == 0
        assert m.every_seconds == 21600
        assert m.every_n_tool_calls == 0
        assert m.max_cycles == 10

    def test_meta_flags(self, real_loader):
        m = real_loader.get("meta")
        assert m is not None
        assert m.trigger == "cold_floor"
        assert m.context_builder == "short_term"
        assert m.grants_task_store is True
        assert m.inject_telemetry is True
        assert m.cold_floor_seconds == 86400
        assert m.fresh_start is True

    def test_invalid_trigger_falls_back_to_periodic(self, tmp_path):
        md = tmp_path / "bad_trigger" / "MODE.md"
        md.parent.mkdir(parents=True)
        md.write_text("---\nname: bt\ntrigger: invalid_value\n---\nbody", encoding="utf-8")
        loader = SubconsciousModeLoader(str(tmp_path))
        loader.load_all()
        m = loader.get("bt")
        assert m is not None
        assert m.trigger == "periodic"

    def test_invalid_pre_compress_context_falls_back_to_full(self, tmp_path):
        md = tmp_path / "bad_pre_context" / "MODE.md"
        md.parent.mkdir(parents=True)
        md.write_text(
            "---\nname: bad_pre_context\npre_compress_context: invalid\n---\nbody",
            encoding="utf-8",
        )
        loader = SubconsciousModeLoader(str(tmp_path))
        loader.load_all()
        assert loader.get("bad_pre_context").pre_compress_context == "full"

    def test_archived_directory_skipped(self, tmp_path):
        # A mode inside archived/ should NOT be loaded.
        (tmp_path / "archived" / "old_mode").mkdir(parents=True)
        (tmp_path / "archived" / "old_mode" / "MODE.md").write_text(
            "---\nname: old_mode\ntrigger: periodic\n---\nbody", encoding="utf-8"
        )
        # A mode outside archived/ should be loaded.
        (tmp_path / "active_mode").mkdir()
        (tmp_path / "active_mode" / "MODE.md").write_text(
            "---\nname: active_mode\ntrigger: periodic\n---\nbody", encoding="utf-8"
        )
        loader = SubconsciousModeLoader(str(tmp_path))
        loader.load_all()
        assert "old_mode" not in loader.list_names()
        assert "active_mode" in loader.list_names()

    def test_purpose_and_retire_after_parsed(self, tmp_path):
        md = tmp_path / "mymode" / "MODE.md"
        md.parent.mkdir(parents=True)
        md.write_text(
            '---\nname: mymode\ntrigger: periodic\n'
            'purpose: "解决某个具体问题"\nretire_after: "2026-12-31 后"\n---\nbody',
            encoding="utf-8",
        )
        loader = SubconsciousModeLoader(str(tmp_path))
        loader.load_all()
        m = loader.get("mymode")
        assert m is not None
        assert m.purpose == "解决某个具体问题"
        assert m.retire_after == "2026-12-31 后"

    def test_protected_field_parsed(self, tmp_path):
        md = tmp_path / "core" / "MODE.md"
        md.parent.mkdir(parents=True)
        md.write_text(
            "---\nname: core\ntrigger: periodic\nprotected: true\n---\nbody",
            encoding="utf-8",
        )
        loader = SubconsciousModeLoader(str(tmp_path))
        loader.load_all()
        m = loader.get("core")
        assert m is not None
        assert m.protected is True

    def test_protected_defaults_false(self, tmp_path):
        md = tmp_path / "plain" / "MODE.md"
        md.parent.mkdir(parents=True)
        md.write_text("---\nname: plain\ntrigger: periodic\n---\nbody", encoding="utf-8")
        loader = SubconsciousModeLoader(str(tmp_path))
        loader.load_all()
        assert loader.get("plain").protected is False

    def test_seed_audit_is_protected(self, real_loader):
        assert real_loader.get("audit").protected is True

    def test_seed_introspect_is_protected(self, real_loader):
        assert real_loader.get("introspect").protected is True

    def test_seed_meta_is_protected(self, real_loader):
        assert real_loader.get("meta").protected is True

    def test_seed_explore_not_protected(self, real_loader):
        assert real_loader.get("explore").protected is False

    def test_seed_garden_has_retire_after(self, real_loader):
        m = real_loader.get("garden")
        assert m is not None
        assert m.retire_after  # non-empty

    def test_seed_audit_has_purpose(self, real_loader):
        m = real_loader.get("audit")
        assert m is not None
        assert m.purpose  # non-empty

    def test_seed_summarize_is_manual(self, real_loader):
        m = real_loader.get("summarize")
        assert m is not None
        assert m.trigger == "manual"
        assert m.pre_compress is True
        assert m.every_n_compressions == 1
        assert m.pre_compress_context == "slice"
        assert m.max_cycles == 10

    def test_seed_english_companions_include_optimized_output_rules(self):
        with locale_context("en"):
            loader = SubconsciousModeLoader(".coworker/subconscious")
            loader.load_all()
        assert "at most once per run" in loader.get("explore").body
        assert "update or enrich it instead of creating another" in loader.get("introspect").body
        assert "For long-running tasks" not in loader.get("audit").body


# ---------------------------------------------------------------------------
# render_identity: placeholder substitution
# ---------------------------------------------------------------------------


class TestRenderIdentity:
    def test_substitutes_all_three_placeholders(self):
        mode = SubconsciousMode(
            name="test",
            body="id={bubble_id} goal={goal} max={max_cycles}",
        )
        bubble = Bubble(id="bbl_abc", goal="my goal", max_cycles=3)
        rendered = mode.render_identity(bubble)
        assert rendered == "id=bbl_abc goal=my goal max=3"

    def test_no_extra_placeholders_replaced(self):
        """str.replace must not touch curly braces that are not our 3 named placeholders."""
        mode = SubconsciousMode(
            name="test",
            body='example: task_create(description="[成长] {placeholder_unknown}")',
        )
        bubble = Bubble(id="bbl_1", goal="g", max_cycles=1)
        rendered = mode.render_identity(bubble)
        # The unknown placeholder should survive unchanged.
        assert "{placeholder_unknown}" in rendered

    def test_goal_placeholder_in_garden_example(self):
        """The garden body uses {goal} in a bubble_send example; it should be the palace name."""
        mode = SubconsciousMode(
            name="garden",
            body="宫殿《{goal}》卡片建议",
        )
        bubble = Bubble(id="b", goal="Jenkins", max_cycles=5)
        assert "《Jenkins》" in mode.render_identity(bubble)

    # ------------------------------------------------------------------
    # Golden comparison tests: zero-drift verification
    # ------------------------------------------------------------------

    def test_audit_zero_drift(self, real_loader):
        m = real_loader.get("audit")
        assert m is not None
        rendered = m.render_identity(_ref_bubble())
        assert rendered == _GOLDEN_AUDIT

    def test_summarize_zero_drift(self, real_loader):
        m = real_loader.get("summarize")
        assert m is not None
        rendered = m.render_identity(_ref_bubble())
        assert rendered == _GOLDEN_SUMMARIZE

    def test_explore_zero_drift(self, real_loader):
        m = real_loader.get("explore")
        assert m is not None
        rendered = m.render_identity(_ref_bubble())
        assert rendered == _GOLDEN_EXPLORE
