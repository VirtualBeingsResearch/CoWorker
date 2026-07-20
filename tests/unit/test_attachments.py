from __future__ import annotations

from coworker.agent.loop import AgentLoop
from coworker.brain.anthropic_provider import AnthropicProvider
from coworker.brain.deepseek_provider import DeepSeekProvider
from coworker.brain.openai_provider import OpenAIProvider
from coworker.brain.qwen_provider import QwenProvider
from coworker.core.types import AttachmentData, IncomingEvent


def _image_att(filename="photo.jpg", path="data/attachments/photo.jpg"):
    return AttachmentData(
        filename=filename,
        media_type="image/jpeg",
        saved_path=path,
        data="base64data",
    )


def _pdf_att(filename="doc.pdf", path="data/attachments/doc.pdf"):
    return AttachmentData(
        filename=filename,
        media_type="application/pdf",
        saved_path=path,
        data="pdfbase64",
    )


def _file_att(filename="notes.txt", path="data/attachments/notes.txt"):
    return AttachmentData(
        filename=filename,
        media_type="text/plain",
        saved_path=path,
        data=None,
    )


def _video_att(filename="clip.mp4", path="data/attachments/clip.mp4"):
    return AttachmentData(
        filename=filename,
        media_type="video/mp4",
        saved_path=path,
        data=None,
    )


def _unknown_att(filename="archive.zip", path="data/attachments/archive.zip"):
    return AttachmentData(
        filename=filename,
        media_type="application/octet-stream",
        saved_path=path,
        data=None,
    )


class TestBuildContentBlocks:
    def test_no_attachments_returns_str(self):
        event = IncomingEvent(participant_id="alice", content="hello")
        result = AgentLoop._build_content_blocks([event])
        assert isinstance(result, str)
        assert "alice" in result
        assert "hello" in result

    def test_image_attachment_returns_list_with_image_block(self):
        event = IncomingEvent(participant_id="alice", content="看图", attachments=[_image_att()])
        result = AgentLoop._build_content_blocks([event])
        assert isinstance(result, list)
        types = [b["type"] for b in result]
        assert "text" in types
        assert "image" in types

    def test_image_block_contains_metadata(self):
        att = _image_att(path="data/attachments/x.jpg")
        event = IncomingEvent(participant_id="alice", content="", attachments=[att])
        result = AgentLoop._build_content_blocks([event])
        assert result[0]["text"] == "[来自文件投递][alice]的消息:\n"
        img_block = next(b for b in result if b["type"] == "image")
        assert img_block["_saved_path"] == "data/attachments/x.jpg"
        assert img_block["_filename"] == "photo.jpg"
        assert img_block["source"]["data"] == "base64data"

    def test_pdf_attachment_returns_document_block(self):
        event = IncomingEvent(participant_id="alice", content="", attachments=[_pdf_att()])
        result = AgentLoop._build_content_blocks([event])
        assert any(b["type"] == "document" for b in result)

    def test_text_file_returns_path_reference(self):
        att = _file_att(path="data/attachments/notes.txt")
        event = IncomingEvent(participant_id="alice", content="", attachments=[att])
        result = AgentLoop._build_content_blocks([event])
        assert isinstance(result, list)
        text_block = next(block for block in result if "notes.txt" in block.get("text", ""))
        assert "notes.txt" in text_block["text"]
        assert "data/attachments/notes.txt" in text_block["text"]

    def test_unknown_file_returns_path_reference(self):
        att = _unknown_att(path="data/attachments/archive.zip")
        event = IncomingEvent(participant_id="bob", content="", attachments=[att])
        result = AgentLoop._build_content_blocks([event])
        assert any("archive.zip" in b.get("text", "") for b in result)
        assert any("data/attachments/archive.zip" in b.get("text", "") for b in result)

    def test_video_attachment_is_path_only_and_labeled(self):
        event = IncomingEvent(participant_id="bob", content="", attachments=[_video_att()])
        result = AgentLoop._build_content_blocks([event])
        assert result == [
            {"type": "text", "text": "[来自文件投递][bob]的消息:\n"},
            {
                "type": "text",
                "text": "[视频附件: clip.mp4 — 已保存至 data/attachments/clip.mp4，可使用工具读取]",
            },
        ]

    def test_content_text_prefix_included_when_nonempty(self):
        event = IncomingEvent(participant_id="carol", content="消息内容", attachments=[_image_att()])
        result = AgentLoop._build_content_blocks([event])
        text_blocks = [b for b in result if b["type"] == "text"]
        combined = " ".join(b["text"] for b in text_blocks)
        assert "carol" in combined
        assert "消息内容" in combined


class TestAdaptContentAnthropic:
    def _provider(self):
        p = AnthropicProvider.__new__(AnthropicProvider)
        p._current_model = "claude-sonnet-4-6"
        return p

    def test_str_content_unchanged(self):
        p = self._provider()
        assert p._adapt_content("hello", "claude-sonnet-4-6") == "hello"

    def test_strips_private_keys(self):
        content = [{"type": "image", "source": {}, "_filename": "x.jpg", "_saved_path": "/tmp/x.jpg"}]
        result = self._provider()._adapt_content(content, "claude-sonnet-4-6")
        assert "_filename" not in result[0]
        assert "_saved_path" not in result[0]
        assert result[0]["type"] == "image"

    def test_passes_document_block_through(self):
        content = [{"type": "document", "source": {"type": "base64"}, "_filename": "f.pdf", "_saved_path": "/tmp/f.pdf"}]
        result = self._provider()._adapt_content(content, "claude-sonnet-4-6")
        assert result[0]["type"] == "document"
        assert "_filename" not in result[0]


class TestAdaptContentOpenAI:
    _VISION_MODEL = "gpt-5.4"

    def _provider(self):
        p = OpenAIProvider.__new__(OpenAIProvider)
        p._current_model = self._VISION_MODEL
        return p

    def _content(self):
        return [
            {"type": "text", "text": "hi"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": "abc"},
                "_filename": "img.jpg",
                "_saved_path": "/tmp/img.jpg",
            },
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": "xyz"},
                "_filename": "doc.pdf",
                "_saved_path": "/tmp/doc.pdf",
            },
        ]

    def test_vision_model_converts_image_to_image_url(self):
        p = self._provider()
        result = p._adapt_content(self._content(), self._VISION_MODEL)
        img = next(b for b in result if b.get("type") == "image_url")
        assert img["image_url"] == "data:image/jpeg;base64,abc"

    def test_vision_model_pdf_includes_saved_path(self):
        p = self._provider()
        result = p._adapt_content(self._content(), self._VISION_MODEL)
        pdf_text = next(b for b in result if b.get("type") == "text" and "doc.pdf" in b.get("text", ""))
        assert "/tmp/doc.pdf" in pdf_text["text"]

    def test_non_vision_model_degrades_image_with_path(self):
        p = self._provider()
        result = p._adapt_content(self._content(), "gpt-3.5-turbo")
        texts = [b.get("text", "") for b in result if b.get("type") == "text"]
        assert any("img.jpg" in t and "/tmp/img.jpg" in t for t in texts)

    def test_non_vision_model_degrades_pdf_with_path(self):
        p = self._provider()
        result = p._adapt_content(self._content(), "gpt-3.5-turbo")
        texts = [b.get("text", "") for b in result if b.get("type") == "text"]
        assert any("doc.pdf" in t and "/tmp/doc.pdf" in t for t in texts)


class TestAdaptContentQwen:
    _VIDEO_MODEL = "qwen3.7-plus"

    def _provider(self):
        provider = QwenProvider.__new__(QwenProvider)
        provider._current_model = self._VIDEO_MODEL
        return provider

    def test_video_capability_is_explicit(self):
        provider = self._provider()
        assert provider.supports_video("qwen3.7-plus") is True
        assert provider.supports_video("qwen3.7-max") is False

    def test_converts_video_to_video_url_without_fps(self):
        content = [{
            "type": "video",
            "source": {"type": "base64", "media_type": "video/mp4", "data": "abc"},
            "_filename": "clip.mp4",
        }]
        result = self._provider()._adapt_content(content, self._VIDEO_MODEL)
        assert result == [{
            "type": "video_url",
            "video_url": {"url": "data:video/mp4;base64,abc"},
        }]
        assert "fps" not in result[0]


class TestAdaptContentDeepSeek:
    def _provider(self):
        p = DeepSeekProvider.__new__(DeepSeekProvider)
        p._current_model = "deepseek-v4-flash"
        return p

    def test_always_degrades_image_with_path(self):
        content = [{
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": "abc"},
            "_filename": "pic.jpg",
            "_saved_path": "data/attachments/pic.jpg",
        }]
        result = self._provider()._adapt_content(content, "deepseek-v4-flash")
        assert result[0]["type"] == "text"
        assert "pic.jpg" in result[0]["text"]
        assert "data/attachments/pic.jpg" in result[0]["text"]

    def test_str_content_unchanged(self):
        assert self._provider()._adapt_content("plain text", "deepseek-v4-flash") == "plain text"
