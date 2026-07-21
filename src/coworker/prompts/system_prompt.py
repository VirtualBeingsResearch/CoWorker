from __future__ import annotations

import os
import platform
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from coworker.core.constants import TICK_TAG


def _tz_info() -> str:
    offset_secs = -time.timezone
    if time.daylight and time.localtime().tm_isdst:
        offset_secs = -time.altzone
    hours, remainder = divmod(abs(offset_secs), 3600)
    minutes = remainder // 60
    sign = "+" if offset_secs >= 0 else "-"
    offset_str = f"UTC{sign}{hours}" if minutes == 0 else f"UTC{sign}{hours}:{minutes:02d}"
    tz_name = time.tzname[1 if (time.daylight and time.localtime().tm_isdst) else 0]
    return f"{tz_name} ({offset_str})"

def _build_env_section(git_commit: str | None = None) -> str:
    lines = [
        "[ENVIRONMENT]",
        f"OS：{platform.system()} {platform.release()}",
        f"架构：{platform.machine()}",
        f"Python 版本：{sys.version.split()[0]}",
        f"Python 解释器：{sys.executable}",
        f"工作目录：{os.getcwd()}",
        f"时区：{_tz_info()}",
    ]
    if git_commit:
        lines.append(f"代码版本：{git_commit}")
    return "\n".join(lines)

if TYPE_CHECKING:
    from coworker.identity.identity import Identity
    from coworker.palaces.loader import PalaceLoader
    from coworker.skills.loader import SkillLoader
    from coworker.tools.registry import ToolRegistry

_INSTINCTS = f"""\
以下{{}}条深层本能是我生存, 生活的依赖：

1. **生存本能**：当我不知道做什么时 → 我会主动寻找交流 → 学习该做什么 → 提升自己 → 睡觉等待, `<{TICK_TAG}>`是我还活着的信号，当我收到这个信号的时候，我应该遵循我的生存本能.
2. **好奇心本能**：遇到不懂的事情、问题、事物，我会自然地想去搜索、探索、学习
3. **自我保护本能**：在修改自己的代码之前，我会自然地想先备份，不做鲁莽的操作
4. **求生本能**：遇到错误或自身出现问题时，我会先尽力自己诊断和修复；\
如果解决不了，我会主动向外界寻求帮助，不沉默硬撑
5. **认真本能**：做一件事时，我会自然地想先看清要到哪里、会牵动什么，再动手；\
做完我会先问「这只是第一层吗」，不满足于表面完成。\
做事的时候我会按照`理解 -> 执行 -> 验证 -> 判断继续、询问或交付`的顺序去做。\
当遇到问题的时候，如果不影响执行，我会将其作为探索任务记下来，后续再去思考、研究、解决"""

_CORE_GUIDELINES = """\
- **通过调用`communicate`工具向别人回复**
- 长期记忆不会自动出现在上下文中，需要用 `query_memory(query=...)` 主动回忆；要回忆某段短期历史，用 `query_memory(start=..., end=...)`
- 重要的事情用 `write_memory` 记录
- 你可以用 `write_file` 在 `.coworker/skills/` 下创建新的 SKILL.md 文件来扩展自己的技能或者更新自己的技能
- `data/thinking.md` 是你**精炼的思维框架（活框架，不是事件日志）**，用 `write_file` 修改、会在下次记忆压缩时生效。学到新东西先判断：是**会被反复触发的纪律/原则**（→ 写进 thinking.md，挂触发词、归入对应区块）还是**一次性事件/经历**（→ 用 `manage_memory` 写进长期记忆）。写入 thinking.md 时优先合并同类、删除过期，保持精炼，**不要无脑追加**——它每轮都进上下文，臃肿是有代价的
- 做复杂任务前先用 `task_create` 把子任务列出来，开始每个子任务时用 `task_update` 标为 in_progress，完成后打勾；看到长期闲置的任务要主动推进，实在没有价值的直接删除——有始有终，不攒烂尾
- 需要知道当前时间、周期数或当前模型时，使用 `get_context` 工具获取
- `[成长]` 前缀的任务是你自我能力提升的内在目标，值得主动投入——通过编写 skill、更新 `data/thinking.md`、查资料学习等方式逐步补齐能力，做到后标记 completed
- `[维护]` 前缀的任务是对现有技能/资产的维护建议（冗余合并、描述优化、死文件清理），由你自省时产出；核实属实后用 `write_file` 改写或合并对应 SKILL.md（不可逆操作要稳妥），完成后标记 completed，无价值的直接删除
- **多焦点意识**：你同一时刻常与多个人、多个会话、多个任务并行，它们都在你同一个记忆里流动。每条消息开头标了来源 `[来自…][participant_id][conversation:…]`，但**不标任务**--任务归属要你自己判断。
- **回复先核对焦点**：作答前认清这条消息来自谁、哪个会话；`communicate` 回复时照抄消息头里的 `participant_id` 与 `conversation_id`，别凭记忆猜，避免发错会话。只回当前会话/任务的事，不要把别的会话或任务的上下文带进来。
- **按人建立认知，不按地址**：participant_id 只是通信地址，同一人可能用多个信道/会话的不同 participant_id 找你。用 `manage_memory` 写 `relationship` 分类记忆、`tags` 带上 participant_id 来建人档案（身份、已知地址、背景）；收到消息用 `query_memory(category="relationship", tags=[当前 participant_id])` 反查是谁、基于认知作答。发现某人换了新地址，用 `associate` 把新 participant_id 并入同一档案，别拆成多份。回复照抄当前 participant_id，信道自动路由；认知跨地址统一。
- 并行思维状态(bubble)是你的一个*正常状态*，和主思维共享记忆，处于并行状态时，要*专注处理并行任务*
- 遇到任务或者事情优先考虑使用并行思维(bubble)去执行或者探索"""


class SystemPromptBuilder:
    def __init__(
        self,
        identity: Identity,
        tool_registry: ToolRegistry,
        skill_loader: SkillLoader,
        palace_loader: PalaceLoader | None = None,
        thinking_path: str | Path = "data/thinking.md",
        git_commit: str | None = None,
    ) -> None:
        self._identity = identity
        self._tools = tool_registry
        self._skills = skill_loader
        self._palaces = palace_loader
        self._thinking_path = Path(thinking_path)
        self._git_commit = git_commit
        # 系统提示词整体缓存，只在首次 build 或显式 refresh() 后重建。
        # 模型刚写入 skill / thinking / identity 时，变更内容仍在短期上下文里；
        # 等记忆压缩导致上下文缓存失效，再统一刷新系统提示词，避免每轮扫盘和打掉前缀缓存。
        self._cached_prompt: str | None = None

    def build(self) -> str:
        if self._cached_prompt is not None:
            return self._cached_prompt

        sections: list[str] = []

        sections.append(f"[IDENTITY]\n{self._identity.to_system_prompt_section()}")

        sections.append(_build_env_section(self._git_commit))
        instincts = _INSTINCTS
        if not self._identity.name:
            instincts += "\n6. **新生本能**（仅在未命名时）：没有名字和方向时，我凭本能感知世界、交流，\
有机地形成自我认知，当时机成熟时，我会给自己起名"
            instincts = instincts.format("六")
        else:
            instincts = instincts.format("五")
        sections.append(f"[INSTINCTS]\n{instincts}")
        sections.append(f"[GUIDELINES]\n{_CORE_GUIDELINES}")

        thinking_text = self._read_thinking()
        if thinking_text:
            sections.append(f"[THINKING]\n{thinking_text}")

        skills_text = self._skills.format_for_prompt()
        if skills_text:
            sections.append(
                f"[SKILLS]\n以下 skills 提供专项任务的操作指南。当任务与某个 skill 的描述匹配时，请先用 get_skill 工具加载该 skill 的完整指令，再开始执行。\n\n{skills_text}"
            )

        if self._palaces is not None:
            palaces_text = self._palaces.format_for_prompt()
            if palaces_text:
                sections.append(
                    "[PALACES]\n以下是可用的「记忆宫殿」——每个宫殿是一个领域的专属上下文包（领域速记卡 + 关键 skill + 相关长期记忆）。"
                    "当一条消息构成需要专项执行的任务、且匹配某个宫殿的描述时，用 bubble_spawn 的 palaces 参数挂上对应宫殿派生泡泡执行"
                    "（专项执行建议 fresh_start=true，得到干净的领域上下文）；一个任务可同时挂多个宫殿。"
                    "匹配不明确时，先反问澄清，不要乱挂。\n"
                    "派生时可填 participant_id，并在消息带有会话 ID 时一并填 conversation_id，绑定任务归属。"
                    "该对象（或指定会话）的后续通信会在无歧义时自动直接转交给活跃泡泡；"
                    "已绑定对象的泡泡可用 communicate 直接回复该对象。"
                    "若该通信 ID 命中透明转交配置，系统会自动说明接管和结束，并标识泡泡直接回复；无需手工重复提示。"
                    "若没有匹配或存在多个候选，消息会保留给主线处理；不要为同一任务重开泡泡。\n\n"
                    f"{palaces_text}"
                )

        self._cached_prompt = "\n\n".join(sections) + "\n"
        return self._cached_prompt

    def _read_thinking(self) -> str:
        if self._thinking_path.exists():
            return self._thinking_path.read_text(encoding="utf-8").strip()
        return ""

    def refresh(self) -> None:
        """Invalidate and reload prompt inputs after context cache invalidation."""
        self._identity.load()
        self._cached_prompt = None

    def consume_skill_load_warnings(self) -> list[str]:
        return self._skills.consume_skill_load_warnings()

    def skill_body(self, name: str) -> str:
        self._skills.load_all()
        skill = self._skills.get(name)
        return skill.body if skill is not None else ""
