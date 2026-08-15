from __future__ import annotations

import base64
import math

from coworker.core.token_utils import DOCUMENT_TOKEN_ESTIMATE, estimate_content_tokens
from coworker.core.types import (
    AttachmentData,
    CommunicateRequest,
    ConversationThread,
    LLMResponse,
    Message,
    estimate_text_tokens,
)


class TestEstimateTextTokens:
    def test_empty_string(self):
        assert estimate_text_tokens("") == 0

    def test_cjk_one_token_per_char(self):
        # each Chinese character is 1 token
        assert estimate_text_tokens("你好世界") == 4
        assert estimate_text_tokens("中文测试内容") == 6

    def test_number_is_one_token(self):
        assert estimate_text_tokens("12345") == 1
        # "3.14" is split at "." → ["3", ".", "14"] → 3 segments → 3 tokens
        assert estimate_text_tokens("3.14") == 3

    def test_short_word_is_one_token(self):
        assert estimate_text_tokens("hi") == 1
        assert estimate_text_tokens("the") == 1

    def test_long_english_word(self):
        # ceil(12 / 6) = 2
        assert estimate_text_tokens("accomplishment") == math.ceil(len("accomplishment") / 6)

    def test_sentence_with_spaces(self):
        # each space segment returns 0, words counted separately
        tokens = estimate_text_tokens("hello world")
        assert tokens == 2  # "hello"→1, " "→0, "world"→1

    def test_cjk_mixed_with_numbers(self):
        tokens = estimate_text_tokens("共2个")
        # "共" → 1, "2" → 1, "个" → 1
        assert tokens == 3

    def test_punctuation_sequence(self):
        # ceil(4/2) = 2
        assert estimate_text_tokens("....") == 2

    def test_cjk_more_accurate_than_len_div_4(self):
        text = "这是一段中文测试文本，共有很多汉字"
        naive = len(text) // 4
        smart = estimate_text_tokens(text)
        # smart should be much higher — closer to actual CJK token count
        assert smart > naive


class TestEstimateDocumentTokens:
    def _pdf_block(self, size_bytes: int) -> dict:
        data = base64.b64encode(b"x" * size_bytes).decode()
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": data,
            },
        }

    def test_no_data_returns_fallback(self):
        block = {"type": "document", "source": {"type": "base64"}}
        assert estimate_content_tokens([block]) == DOCUMENT_TOKEN_ESTIMATE

    def test_small_pdf_at_least_fallback(self):
        # 1 KB PDF → 1024*3/4/40 = 19 < fallback, so should return fallback
        tokens = estimate_content_tokens([self._pdf_block(1_024)])
        assert tokens == DOCUMENT_TOKEN_ESTIMATE

    def test_one_page_pdf(self):
        # 100 KB binary → base64 → binary_bytes ≈ 100_000 → tokens ≈ 2500
        tokens = estimate_content_tokens([self._pdf_block(100_000)])
        assert 2400 <= tokens <= 2600

    def test_large_pdf_scales_with_size(self):
        small = estimate_content_tokens([self._pdf_block(50_000)])
        large = estimate_content_tokens([self._pdf_block(500_000)])
        assert large >= small * 5


class TestAttachmentData:
    def test_create_image_attachment(self):
        att = AttachmentData(
            filename="photo.jpg",
            media_type="image/jpeg",
            saved_path="/tmp/photo.jpg",
            data="abc123",
        )
        assert att.filename == "photo.jpg"
        assert att.data == "abc123"

    def test_create_file_attachment_no_data(self):
        att = AttachmentData(
            filename="notes.txt",
            media_type="text/plain",
            saved_path="/tmp/notes.txt",
        )
        assert att.data is None


class TestMessage:
    def test_to_dict_minimal(self):
        msg = Message(role="user", content="hello")
        d = msg.to_dict()
        assert d == {"role": "user", "content": "hello"}

    def test_to_dict_list_content(self):
        blocks = [{"type": "text", "text": "hi"}, {"type": "image", "source": {}}]
        msg = Message(role="user", content=blocks)
        d = msg.to_dict()
        assert d["content"] == blocks

    def test_content_text_str(self):
        msg = Message(role="user", content="hello")
        assert msg.content_text() == "hello"

    def test_content_text_list(self):
        blocks = [
            {"type": "text", "text": "看图"},
            {"type": "image", "source": {}},
            {"type": "text", "text": "好吗"},
        ]
        msg = Message(role="user", content=blocks)
        assert msg.content_text() == "看图 好吗"

    def test_content_text_list_no_text_blocks(self):
        msg = Message(role="user", content=[{"type": "image", "source": {}}])
        assert msg.content_text() == ""

    def test_to_dict_with_tool_calls(self):
        tc = {"id": "1", "type": "function", "function": {"name": "foo", "arguments": "{}"}}
        msg = Message(role="assistant", content="", tool_calls=[tc])
        d = msg.to_dict()
        assert "tool_calls" in d
        assert d["tool_calls"] == [tc]

    def test_to_dict_with_tool_call_id(self):
        msg = Message(role="tool", content="result", tool_call_id="abc")
        d = msg.to_dict()
        assert d["tool_call_id"] == "abc"

    def test_to_dict_with_reasoning_content(self):
        msg = Message(role="assistant", content="answer", reasoning_content="my thoughts")
        d = msg.to_dict()
        assert d["reasoning_content"] == "my thoughts"

    def test_to_dict_no_reasoning_content_when_none(self):
        msg = Message(role="assistant", content="answer")
        d = msg.to_dict()
        assert "reasoning_content" not in d

    def test_to_dict_with_provider_state(self):
        state = {"kind": "openai_responses", "output_items": [{"id": "rs_1"}]}
        msg = Message(role="assistant", content="", provider_state=state)

        assert msg.to_dict()["provider_state"] == state

    def test_to_dict_omits_empty_tool_calls(self):
        msg = Message(role="assistant", content="hi")
        assert "tool_calls" not in msg.to_dict()


class TestCommunicateRequest:
    def test_to_dict_minimal(self):
        request = CommunicateRequest(participant_id="alice", message="hi")
        assert request.to_dict() == {"participant_id": "alice", "message": "hi"}

    def test_to_dict_with_optional_fields(self):
        request = CommunicateRequest(
            participant_id="alice",
            message="hi",
            conversation_id="thr_1",
            attachments=[{"path": "note.txt"}],
            extra={"mode": "plan"},
        )
        assert request.to_dict() == {
            "participant_id": "alice",
            "message": "hi",
            "conversation_id": "thr_1",
            "attachments": [{"path": "note.txt"}],
            "extra": {"mode": "plan"},
        }


class TestConversationThread:
    def test_add_and_estimate_tokens(self):
        from coworker.core.types import estimate_text_tokens
        thread = ConversationThread(participant_id="alice")
        thread.add(Message(role="user", content="a" * 100))
        thread.add(Message(role="assistant", content="b" * 200))
        tokens = thread.estimate_tokens()
        assert tokens == estimate_text_tokens("a" * 100) + estimate_text_tokens("b" * 200)

    def test_estimate_tokens_list_content(self):
        from coworker.core.types import estimate_text_tokens
        thread = ConversationThread(participant_id="alice")
        # image block should use fixed estimate, not raw base64 length
        big_base64 = "A" * 100_000
        blocks = [
            {"type": "text", "text": "hi"},
            {"type": "image", "source": {"type": "base64", "data": big_base64}},
        ]
        thread.add(Message(role="user", content=blocks))
        tokens = thread.estimate_tokens()
        # "hi" → 1 token (short word), image fixed → 1000, total = 1001
        assert tokens == estimate_text_tokens("hi") + 1000
        # must be far less than naively dividing json length by 4
        import json
        naive = len(json.dumps(blocks)) // 4
        assert tokens < naive // 10

    def test_serialization_round_trip(self):
        thread = ConversationThread(participant_id="bob")
        thread.add(Message(role="user", content="hi"))
        thread.add(
            Message(
                role="assistant",
                content="hello",
                reasoning_content="thinking...",
                provider_state={"kind": "any_llm_message", "signature": "opaque"},
            )
        )
        thread.add(Message(role="tool", content="result", tool_call_id="x1"))

        data = thread.to_dict()
        restored = ConversationThread.from_dict(data)

        assert restored.participant_id == "bob"
        assert len(restored.messages) == 3
        assert restored.messages[1].reasoning_content == "thinking..."
        assert restored.messages[1].provider_state == {
            "kind": "any_llm_message",
            "signature": "opaque",
        }
        assert restored.messages[2].tool_call_id == "x1"

    def test_from_dict_missing_optional_fields(self):
        data = {
            "participant_id": "charlie",
            "messages": [{"role": "user", "content": "hey"}],
            "last_active": "2024-01-01T00:00:00",
        }
        thread = ConversationThread.from_dict(data)
        assert thread.messages[0].tool_call_id is None
        assert thread.messages[0].reasoning_content is None

    def test_summary_defaults(self):
        thread = ConversationThread(participant_id="alice")
        assert thread.summary == ""
        assert thread.summary_message_count == 0

    def test_summary_serialization_round_trip(self):
        thread = ConversationThread(participant_id="alice")
        thread.add(Message(role="user", content="hi"))
        thread.add(Message(role="assistant", content="hello"))
        thread.summary = "alice 想要帮助，我已回应，无后续待办"
        thread.summary_message_count = 2

        data = thread.to_dict()
        assert data["summary"] == "alice 想要帮助，我已回应，无后续待办"
        assert data["summary_message_count"] == 2

        restored = ConversationThread.from_dict(data)
        assert restored.summary == "alice 想要帮助，我已回应，无后续待办"
        assert restored.summary_message_count == 2

    def test_from_dict_missing_summary_fields(self):
        data = {
            "participant_id": "old",
            "messages": [],
            "last_active": "2024-01-01T00:00:00",
        }
        thread = ConversationThread.from_dict(data)
        assert thread.summary == ""
        assert thread.summary_message_count == 0


class TestLLMResponse:
    def test_default_reasoning_content_is_none(self):
        r = LLMResponse(
            content="hi",
            tool_calls=[],
            stop_reason="end_turn",
            model="m",
            usage={},
        )
        assert r.reasoning_content is None

    def test_with_reasoning_content(self):
        r = LLMResponse(
            content="answer",
            tool_calls=[],
            stop_reason="end_turn",
            model="m",
            usage={},
            reasoning_content="chain of thought",
        )
        assert r.reasoning_content == "chain of thought"
