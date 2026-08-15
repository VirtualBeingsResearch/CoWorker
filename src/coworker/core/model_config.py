from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from coworker.core.config import LLMConfig, normalize_thinking_effort


class RuntimeSummaryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = ""
    model: str = ""
    thinking: bool = False
    # 与 LLMConfig 同档位的 canonical effort；空字符串表示沿用 provider 默认。
    thinking_effort: str = ""

    @field_validator("thinking_effort", mode="before")
    @classmethod
    def _normalize_thinking_effort(cls, value: object) -> str:
        return normalize_thinking_effort(value)


class RuntimeVisionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = ""
    model: str = ""
    # 旧版运行态配置没有该字段，应保持此前隐式启用 thinking 的行为。
    thinking: bool = True
    thinking_effort: str = ""

    @field_validator("thinking_effort", mode="before")
    @classmethod
    def _normalize_thinking_effort(cls, value: object) -> str:
        return normalize_thinking_effort(value)


class RuntimeModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 主线思考强度；空字符串表示沿用 provider 默认（与 LLMConfig 同语义）。
    thinking_effort: str = ""
    summary: RuntimeSummaryConfig = Field(default_factory=RuntimeSummaryConfig)
    fallbacks: list[str] = Field(default_factory=list)
    vision: RuntimeVisionConfig = Field(default_factory=RuntimeVisionConfig)

    @field_validator("thinking_effort", mode="before")
    @classmethod
    def _normalize_thinking_effort(cls, value: object) -> str:
        return normalize_thinking_effort(value)

    @classmethod
    def from_llm_config(cls, llm: LLMConfig) -> RuntimeModelConfig:
        return cls(
            thinking_effort=llm.thinking_effort,
            summary=RuntimeSummaryConfig(
                provider=llm.summary_provider,
                model=llm.summary_model,
                thinking=llm.summary_thinking,
                thinking_effort=llm.summary_thinking_effort,
            ),
            fallbacks=list(llm.fallbacks),
            vision=RuntimeVisionConfig(
                provider=llm.vision_provider,
                model=llm.vision_model,
                thinking=llm.vision_thinking,
                thinking_effort=llm.vision_thinking_effort,
            ),
        )

    @classmethod
    def from_brain_snapshot(cls, snapshot: dict) -> RuntimeModelConfig:
        summary = snapshot.get("summary") or {}
        vision = snapshot.get("vision") or {}
        return cls(
            thinking_effort=str(snapshot.get("thinking_effort") or ""),
            summary=RuntimeSummaryConfig(
                provider=str(summary.get("provider") or ""),
                model=str(summary.get("model") or ""),
                thinking=bool(summary.get("thinking")),
                thinking_effort=str(summary.get("thinking_effort") or ""),
            ),
            fallbacks=[str(item) for item in snapshot.get("fallbacks") or []],
            vision=RuntimeVisionConfig(
                provider=str(vision.get("provider") or ""),
                model=str(vision.get("model") or ""),
                thinking=bool(vision.get("thinking", True)),
                thinking_effort=str(vision.get("thinking_effort") or ""),
            ),
        )

    def apply_to_llm_config(self, llm: LLMConfig) -> None:
        llm.thinking_effort = self.thinking_effort
        llm.summary_provider = self.summary.provider
        llm.summary_model = self.summary.model
        llm.summary_thinking = self.summary.thinking
        llm.summary_thinking_effort = self.summary.thinking_effort
        llm.fallbacks = list(self.fallbacks)
        llm.vision_provider = self.vision.provider
        llm.vision_model = self.vision.model
        llm.vision_thinking = self.vision.thinking
        llm.vision_thinking_effort = self.vision.thinking_effort


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
