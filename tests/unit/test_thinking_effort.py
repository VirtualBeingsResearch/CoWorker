from __future__ import annotations

import pytest

from coworker.brain.anthropic_provider import AnthropicProvider
from coworker.brain.deepseek_provider import DeepSeekProvider
from coworker.brain.minimax_provider import MiniMaxProvider
from coworker.brain.openai_compatible_provider import OpenAICompatibleProvider
from coworker.brain.opencode_go_provider import OpenCodeGoProvider
from coworker.brain.qwen_provider import QwenProvider
from coworker.brain.thinking import normalize_thinking_effort, resolve_effort
from coworker.brain.zhipu_provider import ZhipuProvider


def _apply(provider_type: type, model_id: str, *, thinking: bool, effort: str | None) -> dict:
    provider = provider_type.__new__(provider_type)
    provider._current_model = model_id
    kwargs: dict = {}
    provider._apply_thinking(kwargs, resolve_effort(thinking, effort), model_id)
    return kwargs


class TestThinkingEffortNormalization:
    def test_empty_means_unset(self):
        assert normalize_thinking_effort("") is None
        assert normalize_thinking_effort("  HIGH  ") == "high"

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError, match="思考强度"):
            normalize_thinking_effort("ultra")

    def test_false_flag_overrides_effort(self):
        assert resolve_effort(False, "high") == "none"
        assert resolve_effort(True, None) is None


class TestProviderThinkingMappings:
    def test_deepseek_maps_medium_to_high(self):
        kwargs = _apply(DeepSeekProvider, "deepseek-v4-flash", thinking=True, effort="medium")
        assert kwargs["reasoning_effort"] == "high"
        assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}

    def test_deepseek_vision_model_uses_same_thinking_mapping(self):
        kwargs = _apply(
            DeepSeekProvider, "deepseek-v4-flash-vision-exp", thinking=True, effort="medium"
        )
        assert kwargs["reasoning_effort"] == "high"
        assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}

    def test_deepseek_disables_thinking(self):
        kwargs = _apply(DeepSeekProvider, "deepseek-v4-flash", thinking=False, effort="max")
        assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
        assert "reasoning_effort" not in kwargs

    def test_qwen_maps_high_to_xhigh(self):
        kwargs = _apply(QwenProvider, "qwen3.7-plus", thinking=True, effort="high")
        assert kwargs["extra_body"] == {"enable_thinking": True, "reasoning_effort": "xhigh"}

    def test_qwen_disables_thinking(self):
        kwargs = _apply(QwenProvider, "qwen3.7-plus", thinking=False, effort=None)
        assert kwargs["extra_body"] == {"enable_thinking": False}

    def test_minimax_is_two_state(self):
        assert _apply(MiniMaxProvider, "MiniMax-M3", thinking=True, effort="xhigh")["extra_body"] == {
            "reasoning_split": True,
            "thinking": "adaptive",
        }
        assert _apply(MiniMaxProvider, "MiniMax-M3", thinking=False, effort=None)["extra_body"] == {
            "reasoning_split": True,
            "thinking": "disabled",
        }

    def test_zhipu_glm52_passes_reasoning_effort(self):
        kwargs = _apply(ZhipuProvider, "glm-5.2", thinking=True, effort="xhigh")
        assert kwargs["reasoning_effort"] == "max"
        assert kwargs["extra_body"]["thinking"]["type"] == "enabled"

    def test_zhipu_older_model_is_on_off_only(self):
        kwargs = _apply(ZhipuProvider, "glm-4.7", thinking=True, effort="xhigh")
        assert kwargs["extra_body"]["thinking"]["type"] == "enabled"
        assert "reasoning_effort" not in kwargs

    def test_opencode_go_deepseek_mapping(self):
        kwargs = _apply(OpenCodeGoProvider, "deepseek-v4-pro", thinking=True, effort="max")
        assert kwargs["reasoning_effort"] == "max"
        assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}

    def test_opencode_go_kimi_k3_clamps_low_to_high(self):
        kwargs = _apply(OpenCodeGoProvider, "kimi-k3", thinking=True, effort="low")
        assert kwargs["reasoning_effort"] == "high"

    def test_generic_only_sends_explicit_effort(self):
        assert _apply(OpenAICompatibleProvider, "any", thinking=True, effort=None) == {}
        assert _apply(OpenAICompatibleProvider, "any", thinking=True, effort="high") == {
            "reasoning_effort": "high"
        }
        assert _apply(OpenAICompatibleProvider, "any", thinking=False, effort="high") == {}

    def test_anthropic_effort_on_supported_model(self):
        provider = AnthropicProvider.__new__(AnthropicProvider)
        provider._current_model = "claude-opus-4-8"
        kwargs: dict = {}
        provider._apply_thinking(kwargs, resolve_effort(True, "xhigh"))
        assert kwargs == {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "xhigh"},
        }

    def test_anthropic_old_model_ignores_effort(self):
        provider = AnthropicProvider.__new__(AnthropicProvider)
        provider._current_model = "claude-haiku-4-5"
        kwargs: dict = {}
        provider._apply_thinking(kwargs, resolve_effort(True, "xhigh"))
        assert kwargs == {"thinking": {"type": "adaptive"}}
