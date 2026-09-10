from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from coworker.agent.bubble_loop import BubbleMiniLoop
from coworker.agent.inbox_watcher import InboxWatcher
from coworker.agent.loop import AgentLoop
from coworker.channels.activity import ChannelActivityStore
from coworker.channels.base import BaseChannel, ChannelCapabilities
from coworker.channels.progress import (
    ChannelProgressCoordinator,
    ProgressPlaceholder,
    ProgressTransport,
)
from coworker.channels.registry import ChannelRegistry
from coworker.channels.telegram.channel import TelegramChannel
from coworker.channels.telegram.runner import TelegramRunner
from coworker.channels.wecom.channel import WeComChannel
from coworker.channels.wecom.runner import WeComRunner
from coworker.core.config import AgentConfig, TelegramConfig, WeComConfig
from coworker.core.types import (
    AgentState,
    CommunicateRequest,
    IncomingEvent,
    Message,
    ToolResult,
)
from coworker.i18n import locale_context, tr
from coworker.identity.identity import Identity
from coworker.memory.short_term import ShortTermMemory
from coworker.prompts.system_prompt import SystemPromptBuilder
from coworker.skills.loader import SkillLoader
from coworker.tools.file_tools import ReadFileTool
from coworker.tools.registry import ToolRegistry


def _frame_single(request_id: str = "r1", userid: str = "U123", content: str = "ping") -> dict:
    return {
        "headers": {"req_id": request_id},
        "body": {
            "msgid": request_id,
            "chattype": "single",
            "from": {"userid": userid},
            "msgtype": "text",
            "text": {"content": content},
        },
    }


def _frame_group(
    userid: str,
    request_id: str,
    chatid: str = "TEAM",
    content: str = "hi",
) -> dict:
    return {
        "headers": {"req_id": request_id},
        "body": {
            "msgid": request_id,
            "chattype": "group",
            "chatid": chatid,
            "from": {"userid": userid},
            "msgtype": "text",
            "text": {"content": content},
        },
    }


def _agent_config(**kwargs) -> MagicMock:
    config = MagicMock()
    config.agent.channel_progress_enabled = kwargs.get("enabled", True)
    config.agent.channel_progress_reply_reminder_seconds = kwargs.get("reminder", 60)
    config.agent.paused = kwargs.get("paused", False)
    return config


def _wecom_stack(tmp_path, *, enabled: bool = True):
    runner = WeComRunner(
        cfg=WeComConfig(bots={"default": {"enabled": True, "bot_id": "BID", "secret": "SEC"}}),
        attachments_dir=tmp_path,
    )
    bot = runner._bots["default"]
    bot._client = AsyncMock()
    registry = ChannelRegistry()
    registry.register(WeComChannel(runner))
    config = _agent_config(enabled=enabled)
    progress = ChannelProgressCoordinator(registry, config)
    registry.set_progress_coordinator(progress)
    inbox = AsyncMock()
    registry.set_inbound_handler(inbox)
    return runner, bot, registry, progress, inbox


class _TelegramFakeClient:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, int | None]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.deletes: list[tuple[int, int]] = []
        self._next_id = 1

    async def close(self) -> None:
        return None

    async def get_me(self) -> dict:
        return {"id": 1, "is_bot": True}

    async def get_updates(self, offset: int, timeout_seconds: float) -> list[dict]:
        return []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        message_thread_id: int | None = None,
    ) -> int:
        message_id = self._next_id
        self._next_id += 1
        self.messages.append((chat_id, text, message_thread_id))
        return message_id

    async def send_attachment(
        self, chat_id: int, attachment: dict, message_thread_id: int | None = None
    ) -> None:
        return None

    async def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        self.edits.append((chat_id, message_id, text))

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deletes.append((chat_id, message_id))

    async def download_file(self, file_id: str) -> bytes:
        return b"x"


def _telegram_stack(tmp_path: Path, *, enabled: bool = True):
    runner = TelegramRunner(
        TelegramConfig.model_validate({"bots": {"main": {"bot_token": "token"}}}),
        tmp_path / "state",
        tmp_path / "attachments",
        ChannelActivityStore(),
        client_factory=lambda _: _TelegramFakeClient(),
    )
    bot = runner._bots["main"]
    client = _TelegramFakeClient()
    bot._client = client
    bot._ready = True
    registry = ChannelRegistry()
    registry.register(TelegramChannel(runner))
    config = _agent_config(enabled=enabled)
    progress = ChannelProgressCoordinator(registry, config)
    registry.set_progress_coordinator(progress)
    inbox = AsyncMock()
    registry.set_inbound_handler(inbox)
    return runner, bot, client, registry, progress, inbox


def _private_update(
    update_id: int, chat_id: int = 123, user_id: int = 123, text: str = "hello"
) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat_id, "type": "private", "first_name": "Alice"},
            "from": {"id": user_id, "first_name": "Alice"},
            "text": text,
        },
    }


def _group_update(
    update_id: int,
    user_id: int,
    text: str = "hi",
    chat_id: int = -1001,
) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat_id, "type": "supergroup", "title": "Team"},
            "from": {"id": user_id, "first_name": f"User{user_id}"},
            "text": text,
        },
    }


class _OptInProgressChannel(BaseChannel):
    name = "optin"
    participant_prefix = "optin:"

    def __init__(self, *, progress: bool) -> None:
        super().__init__(capabilities=ChannelCapabilities(progress=progress))
        self.opened = 0

    async def send(self, request: CommunicateRequest) -> ToolResult:
        return ToolResult(tool_call_id="", content="sent")

    async def open_progress(
        self,
        event: IncomingEvent,
        text: str,
    ) -> ProgressTransport | None:
        self.opened += 1
        return ProgressTransport(channel=self.name, participant_id=event.participant_id)


def test_agent_config_progress_defaults():
    config = AgentConfig(_env_file=None)
    assert config.channel_progress_enabled is False
    assert config.channel_progress_reply_reminder_seconds == 60
    with pytest.raises(ValidationError):
        AgentConfig(channel_progress_reply_reminder_seconds=-1, _env_file=None)


@pytest.mark.asyncio
async def test_wecom_inbound_skips_placeholder_when_disabled(tmp_path):
    _runner, bot, _registry, _progress, inbox = _wecom_stack(tmp_path, enabled=False)
    await bot._on_text_like(_frame_single())
    inbox.assert_awaited_once()
    bot._client.reply_stream.assert_not_called()
    bot._client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_wecom_inbound_placeholder_then_communicate_overwrites_same_stream(tmp_path):
    _runner, bot, registry, progress, inbox = _wecom_stack(tmp_path)
    frame = _frame_single()
    await bot._on_text_like(frame)
    inbox.assert_awaited_once()
    bot._client.reply_stream.assert_awaited_once()
    open_call = bot._client.reply_stream.await_args
    assert open_call.args[0] is frame
    assert open_call.kwargs["finish"] is False
    stream_id = open_call.args[1]
    bot._client.send_message.assert_not_called()
    assert len(progress.live_placeholders()) == 1

    result = await registry.send(
        CommunicateRequest(participant_id="wecom:default:single:U123", message="正式回复")
    )
    assert result.is_error is False
    assert bot._client.reply_stream.await_count == 2
    overwrite = bot._client.reply_stream.await_args
    assert overwrite.args[0] is frame
    assert overwrite.args[1] == stream_id
    assert overwrite.args[2] == "正式回复"
    assert overwrite.kwargs["finish"] is True
    bot._client.send_message.assert_not_called()
    assert progress.live_placeholders() == []


@pytest.mark.asyncio
async def test_wecom_same_dm_second_inbound_moves_placeholder(tmp_path):
    _runner, bot, registry, progress, _inbox = _wecom_stack(tmp_path)
    first = _frame_single("r1")
    second = _frame_single("r2")
    await bot._on_text_like(first)
    await bot._on_text_like(second)
    assert bot._client.reply_stream.await_count == 3
    close_old, open_second = bot._client.reply_stream.await_args_list[1:]
    assert close_old.args[0] is first
    assert close_old.args[2] == tr("channel.progress.replaced")
    assert close_old.kwargs["finish"] is True
    assert open_second.args[0] is second
    assert open_second.kwargs["finish"] is False
    assert len(progress.live_placeholders()) == 1

    await registry.send(
        CommunicateRequest(participant_id="wecom:default:single:U123", message="ok")
    )
    overwrite = bot._client.reply_stream.await_args
    assert overwrite.args[0] is second
    assert overwrite.args[1] == open_second.args[1]
    assert overwrite.kwargs["finish"] is True


@pytest.mark.asyncio
async def test_wecom_group_inbound_opens_no_placeholder(tmp_path):
    _runner, bot, registry, progress, inbox = _wecom_stack(tmp_path)
    frame = _frame_group("A", "rA")
    await bot._on_text_like(frame)
    inbox.assert_awaited_once()
    bot._client.reply_stream.assert_not_called()
    bot._client.send_message.assert_not_called()
    assert progress.live_placeholders() == []

    # The group frame stays cached, so a reply can still quote it.
    await registry.send(
        CommunicateRequest(
            participant_id="wecom:default:group:TEAM",
            message="reply A",
            conversation_id="rA",
        )
    )
    reply = bot._client.reply_stream.await_args
    assert reply.args[0] is frame
    assert reply.args[2] == "reply A"
    assert reply.kwargs["finish"] is True


@pytest.mark.asyncio
async def test_paused_and_setup_skip_wecom_placeholder(tmp_path):
    _runner, bot, _registry, progress, _inbox = _wecom_stack(tmp_path)
    progress._config.agent.paused = True
    await bot._on_text_like(_frame_single())
    bot._client.reply_stream.assert_not_called()
    progress._config.agent.paused = False
    progress.set_setup_predicate(lambda: True)
    await bot._on_text_like(_frame_single("r2"))
    bot._client.reply_stream.assert_not_called()


@pytest.mark.asyncio
async def test_weixin_source_does_not_send_placeholder(tmp_path):
    _runner, bot, _registry, progress, _inbox = _wecom_stack(tmp_path)
    await progress.on_inbound(
        IncomingEvent(participant_id="weixin:bot1", content="hi", source="weixin")
    )
    bot._client.reply_stream.assert_not_called()
    bot._client.send_message.assert_not_called()


def test_progress_capability_is_limited_to_one_to_one_chats():
    wecom = WeComChannel(MagicMock())
    assert wecom.capabilities_for("wecom:default:single:U123").progress is True
    assert wecom.capabilities_for("wecom:default:group:TEAM").progress is False

    runner = MagicMock()
    runner.contact_for.return_value = None
    telegram = TelegramChannel(runner)
    # An unknown chat falls back to Telegram's ID convention: users are
    # positive, groups, supergroups and channels are negative.
    assert telegram.capabilities_for("tg:main:123").progress is True
    assert telegram.capabilities_for("tg:main:-1001").progress is False

    runner.contact_for.side_effect = lambda participant: SimpleNamespace(
        kind="private" if participant == "tg:main:123" else "supergroup"
    )
    assert telegram.capabilities_for("tg:main:123").progress is True
    assert telegram.capabilities_for("tg:main:-1001").progress is False
    # Only the placeholder bit is withheld; other capabilities are unaffected.
    assert telegram.capabilities_for("tg:main:-1001").attachments is True


def test_progress_close_copy_is_meaningful_in_both_locales():
    for locale in ("en", "zh-CN"):
        with locale_context(locale):
            replaced = tr("channel.progress.replaced")
            superseded = tr("channel.progress.superseded")
            thinking = tr("channel.progress.thinking")
            assert replaced.strip()
            assert superseded.strip()
            assert replaced != "…"
            assert superseded != "…"
            assert replaced != thinking
            assert superseded != thinking


@pytest.mark.asyncio
async def test_progress_opens_only_when_channel_declares_capability():
    off = _OptInProgressChannel(progress=False)
    registry = ChannelRegistry()
    registry.register(off)
    skipped = ChannelProgressCoordinator(registry, _agent_config())
    await skipped.on_inbound(
        IncomingEvent(participant_id="optin:alice", content="hi", source="optin")
    )
    assert off.opened == 0
    assert skipped.live_placeholders() == []

    on = _OptInProgressChannel(progress=True)
    registry_on = ChannelRegistry()
    registry_on.register(on)
    opened = ChannelProgressCoordinator(registry_on, _agent_config())
    await opened.on_inbound(
        IncomingEvent(participant_id="optin:alice", content="hi", source="optin")
    )
    assert on.opened == 1
    assert len(opened.live_placeholders()) == 1


@pytest.mark.asyncio
async def test_telegram_placeholder_edit_and_same_speaker_replace(tmp_path):
    runner, bot, client, registry, progress, inbox = _telegram_stack(tmp_path)
    await bot._consume_update(client, _private_update(1))
    inbox.assert_awaited_once()
    assert client.messages == [(123, tr("channel.progress.thinking"), None)]
    first_id = 1
    assert progress.live_placeholders()[0].transport.telegram_message_id == first_id

    await bot._consume_update(client, _private_update(2, text="again"))
    assert client.deletes == []
    assert client.edits == [(123, first_id, tr("channel.progress.replaced"))]
    assert len(client.messages) == 2
    second_id = 2
    result = await registry.send(CommunicateRequest(participant_id="tg:main:123", message="done"))
    assert result.is_error is False
    assert client.edits == [
        (123, first_id, tr("channel.progress.replaced")),
        (123, second_id, "done"),
    ]


@pytest.mark.asyncio
async def test_telegram_group_inbound_opens_no_placeholder(tmp_path):
    _runner, bot, client, registry, progress, _inbox = _telegram_stack(tmp_path)
    await bot._consume_update(client, _group_update(1, user_id=11))
    await bot._consume_update(client, _group_update(2, user_id=22))
    assert client.messages == []
    assert progress.live_placeholders() == []

    result = await registry.send(
        CommunicateRequest(participant_id="tg:main:-1001", message="to the group")
    )
    assert result.is_error is False
    assert client.edits == []
    assert client.messages == [(-1001, "to the group", None)]


@pytest.mark.asyncio
async def test_reply_reminder_injects_until_communicate(tmp_path):
    _runner, bot, registry, progress, _inbox = _wecom_stack(tmp_path, enabled=True)
    progress._config.agent.channel_progress_reply_reminder_seconds = 10
    await bot._on_text_like(_frame_single())
    progress.live_placeholders()[0].opened_at = time.monotonic() - 30
    due = progress.consume_due_reminders()
    assert due == [("wecom:default:single:U123", due[0][1])]
    assert due[0][1] >= 10
    assert progress.consume_due_reminders() == []

    _runner2, bot2, registry2, progress2, _inbox2 = _wecom_stack(tmp_path / "b")
    progress2._config.agent.channel_progress_reply_reminder_seconds = 10
    await bot2._on_text_like(_frame_single())
    await registry2.send(
        CommunicateRequest(participant_id="wecom:default:single:U123", message="replied")
    )
    progress2.live_placeholders()  # emptied
    assert progress2.consume_due_reminders() == []


@pytest.mark.asyncio
async def test_reply_reminder_zero_disables_injection_only(tmp_path):
    _runner, bot, _registry, progress, _inbox = _wecom_stack(tmp_path)
    progress._config.agent.channel_progress_reply_reminder_seconds = 0
    await bot._on_text_like(_frame_single())
    assert progress.live_placeholders()
    progress.live_placeholders()[0].opened_at = time.monotonic() - 120
    assert progress.consume_due_reminders() == []


def test_system_prompt_progress_section_only_when_enabled(tmp_path):
    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()
    identity = Identity(str(identity_dir))
    identity.load()
    tools = ToolRegistry()
    tools.register(ReadFileTool())
    skills = SkillLoader(str(tmp_path / "skills"))
    (tmp_path / "skills").mkdir()

    class _Channels:
        def agent_instructions(self) -> list[str]:
            return ["WeCom: reachable"]

    with locale_context("en"):
        off = SystemPromptBuilder(
            identity,
            tools,
            skills,
            channel_registry=_Channels(),  # type: ignore[arg-type]
            thinking_path=tmp_path / "thinking.md",
            channel_progress_enabled=False,
        )
        on = SystemPromptBuilder(
            identity,
            tools,
            skills,
            channel_registry=_Channels(),  # type: ignore[arg-type]
            thinking_path=tmp_path / "thinking.md",
            channel_progress_enabled=True,
        )
        off_prompt = off.build()
        on_prompt = on.build()
        note = tr("prompt.channel.progress")
        assert note not in off_prompt
        assert "[CHANNELS]" in off_prompt
        assert note in on_prompt
        on.set_channel_progress_enabled(False)
        assert note not in on.build()


def test_loop_reply_reminder_catalog_placeholders():
    with locale_context("en"):
        text = tr("loop.reply_reminder", participant="wecom:default:single:U123", seconds=60)
    assert "wecom:default:single:U123" in text
    assert "60" in text
    stm = SimpleNamespace(primary=[])
    config = _agent_config(reminder=1)
    progress = ChannelProgressCoordinator(ChannelRegistry(), config)
    progress._placeholders[("dm", "wecom:default:single:U123")] = MagicMock(
        participant_id="wecom:default:single:U123",
        reminded=False,
        opened_at=time.monotonic() - 5,
    )
    injected = progress.inject_reply_reminders(stm)  # type: ignore[arg-type]
    assert injected
    assert isinstance(stm.primary[0], Message)
    assert stm.primary[0].source == "system_reminder"


@pytest.mark.asyncio
async def test_reply_reminder_skips_claimed_placeholder(tmp_path):
    _runner, bot, _registry, progress, _inbox = _wecom_stack(tmp_path)
    progress._config.agent.channel_progress_reply_reminder_seconds = 1
    await bot._on_text_like(_frame_single())
    progress.live_placeholders()[0].opened_at = time.monotonic() - 30
    skipped = progress.consume_due_reminders(
        claimed=lambda item: item.participant_id == "wecom:default:single:U123"
    )
    assert skipped == []
    due = progress.consume_due_reminders()
    assert due and due[0][0] == "wecom:default:single:U123"


# ── 催促注入的位置 ──────────────────────────────────────────────────────────
#
# 催促是 role="user" 的消息，只能在「上一条 assistant[tool_use] 的 tool_result 已
# 经就位」之后注入。插在两者之间会让 provider 拿到
# assistant[tool_use] → user[text] → user[tool_result]，直接拒绝该请求。

def _due_placeholder(participant_id: str) -> ProgressPlaceholder:
    return ProgressPlaceholder(
        participant_id=participant_id,
        conversation_id=None,
        opened_at=time.monotonic() - 120,
        transport=ProgressTransport(channel="telegram"),
    )


def _assert_tool_calls_are_paired(messages: list[Message]) -> None:
    for index, message in enumerate(messages):
        if message.role != "assistant" or not message.tool_calls:
            continue
        expected = ["tool"] * len(message.tool_calls)
        following = [item.role for item in messages[index + 1 : index + 1 + len(expected)]]
        assert following == expected, (
            "每一条 tool_use 后面必须紧跟它自己的 tool_result，实际顺序为 "
            f"{[item.role for item in messages]}"
        )


class _StubBrain:
    """按脚本返回响应，并记录每次请求实际看到的上下文。"""

    current_provider_name = "stub"
    current_model = "stub-model"
    current_model_has_vision = False
    thinking = False
    thinking_effort = None

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.requests: list[list[Message]] = []

    async def think(self, messages, system_prompt, tools) -> SimpleNamespace:
        self.requests.append(list(messages))
        return self._responses.pop(0)


def _tool_use_response() -> SimpleNamespace:
    return SimpleNamespace(
        content="",
        reasoning_content=None,
        tool_calls=[SimpleNamespace(id="t1", name="list_connections", arguments={})],
        stop_reason="tool_use",
        model="stub-model",
        usage={"input_tokens": 1, "output_tokens": 1},
    )


def _main_loop_for_reminder(tmp_path: Path, progress) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    config = MagicMock()
    config.agent.paused = False
    config.agent.passive_mode = False
    config.agent.tick = False
    config.agent.inbox_batch_max = 10
    loop._config = config
    loop._inbox = InboxWatcher(str(tmp_path / "inbox"))
    loop._short_term = ShortTermMemory()
    loop._long_term = MagicMock()
    loop._long_term.is_ready.return_value = False
    loop._tools = MagicMock()
    loop._tools.get_schemas.return_value = []
    loop._tools.execute = AsyncMock(return_value=ToolResult(tool_call_id="t1", content="ok"))
    loop._prompt_builder = MagicMock()
    loop._prompt_builder.build.return_value = "system"
    loop._prompt_builder.consume_skill_load_warnings.return_value = []
    loop._brain = _StubBrain([_tool_use_response()])
    loop._progress = progress
    loop._ilog = None
    loop._snapshot_path = None
    loop._task_store = None
    loop._bubble_store = None
    loop._subconscious = None
    loop._persona = None
    loop._last_compress_generation = loop._short_term.compress_generation
    loop.state = AgentState(current_provider="stub", current_model="stub-model")
    return loop


@pytest.mark.asyncio
async def test_main_cycle_keeps_tool_results_attached_to_their_tool_calls(tmp_path):
    progress = ChannelProgressCoordinator(ChannelRegistry(), _agent_config())
    progress._placeholders["tg:main:123"] = _due_placeholder("tg:main:123")
    loop = _main_loop_for_reminder(tmp_path, progress)

    await loop._cycle()

    primary = loop._short_term.primary
    assert [message for message in primary if message.source == "system_reminder"], (
        "到期的占位必须注入催促，否则这个用例没有覆盖要防的场景"
    )
    assert [message for message in primary if message.tool_calls], "脚本响应应带一个工具调用"
    _assert_tool_calls_are_paired(primary)


def _bubble_loop_for_reminder(progress, *, participant_id: str) -> BubbleMiniLoop:
    loop = BubbleMiniLoop.__new__(BubbleMiniLoop)
    loop._bubble = SimpleNamespace(participant_id=participant_id)
    # BubbleMiniLoop 通过只读属性暴露 _short_term，真实字段是 _stm。
    loop._stm = ShortTermMemory()
    loop._ilog = None
    loop._communicate = SimpleNamespace(_channels=SimpleNamespace(_progress=progress))
    return loop


def test_unbound_bubble_leaves_reply_reminders_for_the_main_loop():
    progress = ChannelProgressCoordinator(ChannelRegistry(), _agent_config())
    progress._placeholders["wecom:default:single:U1"] = _due_placeholder(
        "wecom:default:single:U1"
    )
    bubble_loop = _bubble_loop_for_reminder(progress, participant_id="")

    bubble_loop._inject_reply_reminders()

    assert bubble_loop._short_term.primary == []
    assert progress.consume_due_reminders() == [("wecom:default:single:U1", 120)]


def test_bound_bubble_consumes_its_own_reply_reminder():
    progress = ChannelProgressCoordinator(ChannelRegistry(), _agent_config())
    progress._placeholders["wecom:default:single:U1"] = _due_placeholder(
        "wecom:default:single:U1"
    )
    progress._placeholders["wecom:default:single:U2"] = _due_placeholder(
        "wecom:default:single:U2"
    )
    bubble_loop = _bubble_loop_for_reminder(
        progress, participant_id="wecom:default:single:U1"
    )

    bubble_loop._inject_reply_reminders()

    assert [message.source for message in bubble_loop._short_term.primary] == [
        "system_reminder"
    ]
    assert progress.consume_due_reminders() == [("wecom:default:single:U2", 120)]
