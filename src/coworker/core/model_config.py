from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from coworker.core.config import LLMConfig


class RuntimeSummaryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = ""
    model: str = ""
    thinking: bool = False


class RuntimeVisionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = ""
    model: str = ""
    # 旧版运行态配置没有该字段，应保持此前隐式启用 thinking 的行为。
    thinking: bool = True


class RuntimeModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: RuntimeSummaryConfig = Field(default_factory=RuntimeSummaryConfig)
    fallbacks: list[str] = Field(default_factory=list)
    vision: RuntimeVisionConfig = Field(default_factory=RuntimeVisionConfig)

    @classmethod
    def from_llm_config(cls, llm: LLMConfig) -> RuntimeModelConfig:
        return cls(
            summary=RuntimeSummaryConfig(
                provider=llm.summary_provider,
                model=llm.summary_model,
                thinking=llm.summary_thinking,
            ),
            fallbacks=list(llm.fallbacks),
            vision=RuntimeVisionConfig(
                provider=llm.vision_provider,
                model=llm.vision_model,
                thinking=llm.vision_thinking,
            ),
        )

    @classmethod
    def from_brain_snapshot(cls, snapshot: dict) -> RuntimeModelConfig:
        summary = snapshot.get("summary") or {}
        vision = snapshot.get("vision") or {}
        return cls(
            summary=RuntimeSummaryConfig(
                provider=str(summary.get("provider") or ""),
                model=str(summary.get("model") or ""),
                thinking=bool(summary.get("thinking")),
            ),
            fallbacks=[str(item) for item in snapshot.get("fallbacks") or []],
            vision=RuntimeVisionConfig(
                provider=str(vision.get("provider") or ""),
                model=str(vision.get("model") or ""),
                thinking=bool(vision.get("thinking", True)),
            ),
        )

    def apply_to_llm_config(self, llm: LLMConfig) -> None:
        llm.summary_provider = self.summary.provider
        llm.summary_model = self.summary.model
        llm.summary_thinking = self.summary.thinking
        llm.fallbacks = list(self.fallbacks)
        llm.vision_provider = self.vision.provider
        llm.vision_model = self.vision.model
        llm.vision_thinking = self.vision.thinking


def load_runtime_model_config(path: str | Path) -> RuntimeModelConfig | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"读取运行态模型配置 {path} 失败：{e}") from e
    try:
        return RuntimeModelConfig.model_validate(raw)
    except Exception as e:
        raise ValueError(f"运行态模型配置 {path} 格式无效：{e}") from e


def apply_runtime_model_config_file(llm: LLMConfig) -> RuntimeModelConfig | None:
    runtime = load_runtime_model_config(llm.runtime_config_file)
    if runtime is not None:
        runtime.apply_to_llm_config(llm)
    return runtime


def write_runtime_model_config(path: str | Path, config: RuntimeModelConfig) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(config.model_dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
