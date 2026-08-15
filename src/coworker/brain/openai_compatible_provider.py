from __future__ import annotations

from typing import Any

from coworker.brain.openai_chat import OpenAIChatCompletionsProvider
from coworker.brain.thinking import ThinkingEffort


class OpenAICompatibleProvider(OpenAIChatCompletionsProvider):
    """Generic OpenAI-compatible chat.completions provider.

    Configure it through ``providers.json`` with ``type``:
    ``openai_compatible``, a ``base_url`` and ``model_capabilities``. Tool/vision
    capabilities are administrator-declared; no static model catalog is shipped.
    """

    provider_type = "openai_compatible"
    _DEFAULT_MODEL = ""

    def _apply_thinking(
        self,
        kwargs: dict[str, Any],
        effort: ThinkingEffort | None,
        model_id: str,
    ) -> None:
        # Many OpenAI-compatible gateways accept the standard reasoning_effort
        # field. Only send it when an explicit canonical effort was configured;
        # an unset effort leaves the endpoint's default request shape untouched.
        if effort is not None and effort != "none":
            kwargs["reasoning_effort"] = effort

    def list_models(self) -> list[str]:
        return []

    def supports_tool_use(self, model_id: str) -> bool:
        return True

    def supports_vision(self, model_id: str) -> bool:
        return False
