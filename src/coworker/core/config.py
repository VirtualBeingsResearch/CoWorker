from __future__ import annotations

import json
import math
import os
import re
import secrets
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from coworker.core.constants import (
    DEFAULT_BUBBLE_HANDOFF_TRANSPARENCY_PARTICIPANT_MATCHES,
    DEFAULT_BUBBLE_HANDOFF_TRANSPARENCY_STREAM_TRANSPORTS,
    DEFAULT_LLM_MAX_TOKENS,
    THINKING_EFFORT_LEVELS,
)
from coworker.i18n import SupportedLocale, normalize_locale, tr
from coworker.prompts.template import (
    MAX_SYSTEM_PROMPT_TEMPLATE_CHARS,
    SystemPromptTemplateError,
    validate_system_prompt_template,
)

# 扁平字段（LLM__<TYPE>_API_KEY / _BASE_URL）支持的内置 provider 类型，
# 用于把老式扁平配置自动展开成 name==type 的默认命名实例。
# key 为 provider type，value 为 pydantic 字段前缀（type 含连字符时不同）。
_FLAT_PROVIDER_TYPES = {
    "anthropic": "anthropic",
    "openai": "openai",
    "deepseek": "deepseek",
    "qwen": "qwen",
    "zhipu": "zhipu",
    "minimax": "minimax",
    "opencode-go": "opencode_go",
}
_GITHUB_REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)
_HANDOFF_MATCHES_KEY = "bubble_handoff_transparency_participant_matches"
_LEGACY_HANDOFF_DEFAULTS = (
    (
        "wecom:*",
        "coworker-desktop:*:local:*",
    ),
    (
        "wecom:*",
        "weixin:*",
        "coworker-desktop:*:local:*",
    ),
)


def normalize_thinking_effort(value: object) -> str:
    """Validate a canonical thinking-effort setting; empty string means unset."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(tr("config.thinking.effort_invalid", value=value, levels=", ".join(THINKING_EFFORT_LEVELS)))
    text = value.strip().lower()
    if not text:
        return ""
    if text not in THINKING_EFFORT_LEVELS:
        raise ValueError(tr("config.thinking.effort_invalid", value=value, levels=", ".join(THINKING_EFFORT_LEVELS)))
    return text


class ModelCapabilities(BaseModel):
    """Capabilities that Coworker can rely on for a model."""

    model_config = ConfigDict(extra="forbid")

    tools: bool = False
    vision: bool = False
    video: bool = False

    @model_validator(mode="after")
    def _video_requires_vision(self) -> ModelCapabilities:
        if self.video and not self.vision:
            raise ValueError(tr("config.provider.video_requires_vision"))
        return self


class ModelCapabilitySpec(ModelCapabilities):
    """Administrator-declared capabilities for one model on a provider connection."""

    model: str = Field(min_length=1, max_length=256)

    @field_validator("model")
    @classmethod
    def _normalize_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError(tr("config.provider.model_required"))
        return value


class ProviderSpec(BaseModel):
    """一个命名 provider 实例的配置规格。

    name 是注册名（Brain 注册表 key、default_provider/switch_model 引用的名字），
    type 决定 API 方言/模型表。同一 type 可有多个不同 name 的实例。
    """

    name: str
    type: str
    api_key: str = ""
    base_url: str = ""
    default_model: str | None = None
    tool_use_models: list[str] = Field(default_factory=list)
    model_capabilities: list[ModelCapabilitySpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_model_capabilities(self) -> ProviderSpec:
        seen: set[str] = set()
        for capability in self.model_capabilities:
            if capability.model in seen:
                raise ValueError(
                    tr(
                        "config.provider.duplicate_model_capability",
                        model=capability.model,
                    )
                )
            seen.add(capability.model)
        return self


class ModelPriceSpec(BaseModel):
    """Administrator-defined price for one exact provider/model pair."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(max_length=128)
    model: str = Field(max_length=256)
    currency: str = Field(max_length=3)
    input_per_million: float
    output_per_million: float
    cached_input_per_million: float | None = None

    @field_validator("provider", "model", mode="before")
    @classmethod
    def _normalize_required_part(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError(tr("config.model_price.provider_and_model_required"))
        value = value.strip()
        if not value:
            raise ValueError(tr("config.model_price.provider_and_model_required"))
        return value

    @field_validator("currency", mode="before")
    @classmethod
    def _normalize_currency(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError(tr("config.model_price.currency_invalid"))
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError(tr("config.model_price.currency_invalid"))
        return value

    @field_validator(
        "input_per_million",
        "output_per_million",
        "cached_input_per_million",
    )
    @classmethod
    def _validate_price(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError(tr("config.model_price.price_non_negative"))
        return value


class _EnvSettings(BaseSettings):
    """所有配置类的基类：让 .env 文件优先于 OS 环境变量。

    pydantic-settings 默认优先级是 env_settings > dotenv_settings，
    会导致 shell/容器里残留的环境变量覆盖 .env。这里把两者顺序对调，
    使 .env 成为最高优先级（仅次于显式传参 init_settings）。
    """

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # 顺序靠前者优先：init > .env > 环境变量 > secrets
        return (init_settings, dotenv_settings, env_settings, file_secret_settings)


class LLMConfig(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="LLM__", env_file=".env", extra="ignore")

    default_provider: str = "deepseek"
    default_model: str = "deepseek-v4-pro"
    max_tokens: int = Field(DEFAULT_LLM_MAX_TOKENS, gt=0)
    # 主线思考强度。空字符串沿用 provider 默认请求形状（历史行为），
    # 否则取值 none/minimal/low/medium/high/xhigh/max，由各 provider 映射。
    thinking_effort: str = ""
    summary_provider: str = ""
    summary_model: str = ""
    summary_thinking: bool = False
    summary_thinking_effort: str = ""

    @field_validator("thinking_effort", "summary_thinking_effort", "vision_thinking_effort", mode="before")
    @classmethod
    def _normalize_thinking_effort(cls, value: object) -> str:
        return normalize_thinking_effort(value)

    @model_validator(mode="before")
    @classmethod
    def _opencode_go_key_fallback(cls, data: Any) -> Any:
        """接受官方 OPENCODE_API_KEY 作为 OpenCode Go 密钥的兜底来源。"""
        if isinstance(data, dict) and not data.get("opencode_go_api_key"):
            official = os.environ.get("OPENCODE_API_KEY", "").strip()
            if official:
                data = dict(data)
                data["opencode_go_api_key"] = official
        return data

    # 主模型调用失败后的降级链（有序）。每项为 "providerName" 或 "providerName/modelId"；
    # 省略 modelId 时用该 provider 实例的 default_model。环境变量 LLM__FALLBACKS 传 JSON 数组，
    # 如 LLM__FALLBACKS='["zhipu-userB","deepseek/deepseek-chat"]'。降级后停在备用模型，等手动切回。
    fallbacks: list[str] = Field(default_factory=list)

    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    openai_api_key: str = ""
    openai_base_url: str = ""
    deepseek_api_key: str = ""
    deepseek_base_url: str = ""
    qwen_api_key: str = ""
    qwen_base_url: str = ""
    zhipu_api_key: str = ""
    zhipu_base_url: str = ""
    minimax_api_key: str = ""
    minimax_base_url: str = ""
    opencode_go_api_key: str = ""
    opencode_go_base_url: str = ""

    # 独立的命名 provider 列表文件（JSON 数组，每项 {name,type,api_key,base_url,default_model?}）。
    # 文件不存在则忽略；其条目按 name 覆盖/扩展上面的扁平默认实例，支持同类型多实例。
    providers_file: str = "providers.json"
    runtime_config_file: str = "data/model_runtime_config.json"
    # 管理控制台维护的命名实例；由 admin_config.json 持久化，按 name 覆盖其他来源。
    managed_providers: list[ProviderSpec] = Field(default_factory=list)
    # 按精确 provider/model 匹配的每百万 Token 价格；与 Provider 来源相互独立。
    model_prices: list[ModelPriceSpec] = Field(default_factory=list)

    vision_provider: str = ""
    vision_model: str = ""
    # 保持历史视觉分析默认启用 thinking；可设为 false 以降低延迟和成本。
    vision_thinking: bool = True
    vision_thinking_effort: str = ""

    @model_validator(mode="after")
    def _unique_model_prices(self) -> LLMConfig:
        seen: set[tuple[str, str]] = set()
        for price in self.model_prices:
            key = (price.provider, price.model)
            if key in seen:
                raise ValueError(
                    tr(
                        "config.model_price.duplicate",
                        provider=price.provider,
                        model=price.model,
                    )
                )
            seen.add(key)
        return self

    def resolved_providers(self) -> list[ProviderSpec]:
        """合并「扁平字段展开的默认实例」与「providers_file 中的命名实例」。

        - 扁平字段：每个非空 <type>_api_key 产出一个 name==type 的默认实例。
        - 文件条目：按 name 覆盖同名默认、或新增命名实例（如多个智谱）。
        返回按插入顺序去重后的规格列表。type 是否受支持留给工厂校验。
        """
        specs: dict[str, ProviderSpec] = {}
        for type_, field_prefix in _FLAT_PROVIDER_TYPES.items():
            api_key = getattr(self, f"{field_prefix}_api_key", "")
            if api_key:
                specs[type_] = ProviderSpec(
                    name=type_,
                    type=type_,
                    api_key=api_key,
                    base_url=getattr(self, f"{field_prefix}_base_url", ""),
                )

        for spec in self._load_provider_file():
            specs[spec.name] = spec

        for spec in self.managed_providers:
            specs[spec.name] = spec

        return list(specs.values())

    def _load_provider_file(self) -> list[ProviderSpec]:
        path = Path(self.providers_file)
        if not self.providers_file or not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ValueError(f"读取 providers 文件 {path} 失败：{e}") from e
        if not isinstance(raw, list):
            raise ValueError(f"providers 文件 {path} 顶层必须是 JSON 数组")

        specs: list[ProviderSpec] = []
        for i, item in enumerate(raw):
            spec = ProviderSpec.model_validate(item)
            if not spec.name or not spec.type:
                raise ValueError(f"providers 文件第 {i} 项缺少 name 或 type：{item!r}")
            specs.append(spec)
        return specs


MemoryBackend = Literal["mem0", "file"]


def _default_memory_backend() -> MemoryBackend:
    """长期记忆后端的兜底默认值，可被 MEMORY_DEFAULT_BACKEND 覆盖。

    仅在变量取值为合法后端时生效；否则回退到 mem0，避免无效镜像默认值破坏启动。
    """
    value = os.getenv("MEMORY_DEFAULT_BACKEND", "mem0")
    if value == "file":
        return "file"
    return "mem0"


class MemoryConfig(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="MEMORY__", env_file=".env", extra="ignore")

    db_path: str = "data/memory"
    # 长期记忆后端：mem0 为默认后端，file 为最简文件存储后端。
    # 默认值可被 MEMORY_DEFAULT_BACKEND 覆盖（例如 lite-offline 镜像默认使用 file）；
    # 显式设置 MEMORY__BACKEND（.env 或环境变量）时仍以其为准。
    backend: MemoryBackend = Field(default_factory=_default_memory_backend)
    short_term_max_tokens: int = Field(default=120_000, gt=0)
    # 每次自动压缩处理当前 primary 中最旧消息的 token 比例；tree/legacy 共用。
    compress_ratio: float = Field(default=0.30, gt=0, lt=1)

    # 多分辨率记忆块树（Memory Block Tree）。tree_enabled=False 回退到旧的单锚点压缩。
    tree_enabled: bool = True
    tree_spine_cap_fraction: float = (
        0.30  # 脊柱 token 上限占比；唯一预算旋钮，节点预算/K 均由它导出
    )
    tree_backfill_max_leaves: int = 64  # `--backfill-tree` 一次性回溯生成叶子数上限
    tree_backfill_concurrency: int = 5  # 回溯时叶子摘要/归约合并的并发上限
    tree_merge_reach_depth: int = 2  # 高层合并向下够细层数：2=低两层、1=仅直接子摘要

    auto_recall_enabled: bool = True
    auto_recall_relevance_threshold: float = Field(default=0.5, ge=0, le=1)
    auto_recall_limit: int = Field(default=5, gt=0)

    # mem0 的独立 LLM 配置。留空表示跟随主线（llm.default_provider / 该 provider 的
    # default_model，无则 llm.default_model），与摘要/压缩的跟随逻辑一致。
    mem0_llm_provider: str = ""
    mem0_llm_model: str = ""
    # 独立 thinking 开关（默认关闭，可设为 true；无 None 态）。抽取是结构化 JSON 任务，
    # 默认关闭可避免思考模型把 token 预算耗在推理上导致 content=None。
    mem0_llm_thinking: bool = False
    mem0_embedder_model: str = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"

    # 可选的 Person 子机制：关闭时与现状完全一致。状态与其他记忆状态同放 data/memory。
    persona_enabled: bool = True
    persona_store_path: str = "data/memory/persons.json"


class APIConfig(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="API__", env_file=".env", extra="ignore")

    # Bind to loopback by default.  Public deployments should put an explicit
    # reverse proxy/TLS boundary in front of the API instead of exposing the
    # development server directly.
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65_535)
    public_url: str = Field(default="", max_length=2048)
    communication_token: str = ""
    # Extra OpenAI-channel tokens: stable short name → secret.
    # JSON object in environment/.env, e.g. {"cursor":"cwct_v1_..."}.
    communication_tokens: dict[str, str] = Field(default_factory=dict)
    compat_timeout_seconds: int = Field(default=180, ge=1, le=3600)
    # JSON list in environment/.env, e.g.
    # ["https://desktop.example", "http://127.0.0.1:1420"]
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ]
    )

    @field_validator("public_url")
    @classmethod
    def _validate_public_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return ""
        parsed = urlsplit(value)
        try:
            parsed.port
        except ValueError as error:
            raise ValueError(tr("config.api.public_url_absolute")) from error
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(tr("config.api.public_url_absolute"))
        if parsed.hostname in {"0.0.0.0", "::"}:
            raise ValueError(tr("config.api.public_url_wildcard"))
        if (
            parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(tr("config.api.public_url_origin_only"))
        return value

    @field_validator("communication_tokens", mode="before")
    @classmethod
    def _validate_communication_tokens(cls, value: object) -> object:
        from coworker.core.communication_tokens import validate_token_name

        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(tr("config.api.communication_tokens_must_be_object"))
        normalized: dict[str, str] = {}
        for raw_name, raw_secret in value.items():
            if not isinstance(raw_name, str):
                raise ValueError(tr("config.api.token_name_invalid", name=raw_name))
            name = validate_token_name(raw_name)
            secret = str(raw_secret or "").strip()
            if not secret:
                continue
            normalized[name] = secret
        return normalized


class RelayConfig(_EnvSettings):
    """Outbound connection to a self-hosted Coworker Relay."""

    model_config = SettingsConfigDict(env_prefix="RELAY__", env_file=".env", extra="ignore")

    enabled: bool = False
    url: str = ""
    instance_id: str = ""
    instance_private_key: str = Field(default="", repr=False)
    relay_public_key: str = ""
    auth_epoch: int = Field(default=0, ge=0)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return ""
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("relay url must be an absolute HTTP(S) URL")
        if (
            parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "relay url must not contain credentials, path, query, or fragment"
            )
        return value

    @field_validator("instance_id")
    @classmethod
    def _validate_instance_id(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"cw_[A-Za-z0-9_-]{8,80}", value):
            raise ValueError("relay instance_id has an invalid format")
        return value


class DesktopUpdateSourceBase(BaseModel):
    """A named upstream connection.  ``id`` is the durable identity; ``name`` is display-only."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    token: str = Field(default="", repr=False)
    include_prereleases: bool = False
    # When enabled, a newly imported draft is published automatically right
    # after a successful sync import (instead of staying a draft for a manual
    # admin publish). Opt-in so the default keeps the manual-review workflow.
    auto_publish: bool = False

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("desktop update source name must not be empty")
        if len(value) > 120:
            raise ValueError("desktop update source name must not exceed 120 characters")
        return value


class GitHubDesktopUpdateSource(DesktopUpdateSourceBase):
    type: Literal["github"]
    api_base_url: str = "https://api.github.com"
    repository: str = ""
    include_drafts: bool = False

    @field_validator("repository")
    @classmethod
    def _validate_repository(cls, value: str) -> str:
        value = value.strip().strip("/")
        if value and not _GITHUB_REPOSITORY_RE.fullmatch(value):
            raise ValueError("repository must use a safe owner/name form")
        return value

    @field_validator("api_base_url")
    @classmethod
    def _validate_api_base_url(cls, value: str) -> str:
        return _normalize_source_base_url(value, field_name="api_base_url", allow_empty=False)


class CoworkerDesktopUpdateSource(DesktopUpdateSourceBase):
    type: Literal["coworker"]
    base_url: str = ""

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        return _normalize_source_base_url(value, field_name="base_url", allow_empty=True)


DesktopUpdateSourceSpec = Annotated[
    GitHubDesktopUpdateSource | CoworkerDesktopUpdateSource,
    Field(discriminator="type"),
]


def _normalize_source_base_url(value: str, *, field_name: str, allow_empty: bool) -> str:
    value = value.strip().rstrip("/")
    if not value and allow_empty:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{field_name} must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{field_name} must not contain credentials, query, or fragment")
    return value


class DesktopUpdatesConfig(_EnvSettings):
    model_config = SettingsConfigDict(
        env_prefix="DESKTOP_UPDATES__",
        env_file=".env",
        extra="ignore",
    )

    dir: str = "data/desktop_updates"
    admin_token: str = ""
    sync_sources: list[DesktopUpdateSourceSpec] = Field(default_factory=list)
    sync_active_source: UUID | None = None
    feed_token: str = Field(default="", repr=False)
    sync_interval_seconds: int = Field(default=6 * 60 * 60, ge=300, le=7 * 24 * 60 * 60)
    sync_on_start: bool = True
    sync_max_asset_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=1024)
    sync_max_run_bytes: int = Field(default=4 * 1024 * 1024 * 1024, ge=1024)

    @model_validator(mode="after")
    def _validate_sources(self) -> DesktopUpdatesConfig:
        source_ids = [source.id for source in self.sync_sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("desktop update source IDs must be unique")
        normalized_names = [source.name.casefold() for source in self.sync_sources]
        if len(normalized_names) != len(set(normalized_names)):
            raise ValueError("desktop update source names must be unique")
        if self.sync_active_source is not None and self.sync_active_source not in source_ids:
            raise ValueError("sync_active_source must reference a configured source")
        if self.sync_max_run_bytes < self.sync_max_asset_bytes:
            raise ValueError("sync_max_run_bytes must be at least sync_max_asset_bytes")
        return self

    def active_source(self) -> DesktopUpdateSourceSpec | None:
        if self.sync_active_source is None:
            return None
        return next(
            (source for source in self.sync_sources if source.id == self.sync_active_source),
            None,
        )


class AdminConfig(_EnvSettings):
    """管理控制台配置。

    ``token`` 只从启动配置读取；管理页永远不会回显它。``config_file`` 是管理页
    保存的托管覆盖层，优先级高于 .env，但低于模型运行态热更新文件。
    """

    model_config = SettingsConfigDict(env_prefix="ADMIN__", env_file=".env", extra="ignore")

    token: str = ""
    config_file: str = "data/admin_config.json"


class I18NConfig(_EnvSettings):
    """Instance-wide runtime language, independent from the Web UI."""

    model_config = SettingsConfigDict(env_prefix="I18N__", env_file=".env", extra="ignore")

    locale: SupportedLocale = SupportedLocale.ZH_CN

    @field_validator("locale", mode="before")
    @classmethod
    def _normalize_locale(cls, value: object) -> SupportedLocale:
        if isinstance(value, SupportedLocale):
            return value
        return normalize_locale(str(value))

class AgentConfig(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT__", env_file=".env", extra="ignore")

    inbox_dir: str = "data/inbox"
    outbox_dir: str = "data/outbox"
    desktop_registry_dir: str = "data/coworker_desktop/registry"
    identity_dir: str = "data/identity"
    logs_dir: str = "data/logs"
    interaction_log_rotation_bytes: int = 50 * 1024 * 1024
    skills_dir: str = ".coworker/skills"
    palaces_dir: str = ".coworker/palaces"
    subconscious_dir: str = ".coworker/subconscious"
    system_prompt_template: str = ""

    @field_validator("system_prompt_template", mode="before")
    @classmethod
    def _validate_system_prompt_template(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        try:
            return validate_system_prompt_template(value)
        except SystemPromptTemplateError as error:
            if error.code == "too_long":
                raise ValueError(
                    tr(
                        "config.system_prompt.too_long",
                        limit=MAX_SYSTEM_PROMPT_TEMPLATE_CHARS,
                    )
                ) from error
            raise ValueError(
                tr(
                    f"config.system_prompt.{error.code}",
                    variable=error.variable,
                )
            ) from error

    idle_sleep_seconds: int = Field(default=30, ge=0)
    inbox_poll_interval: float = 2.0
    inbox_batch_max: int = 10
    tick: bool = True
    # passive 模式：_rest() 不设 idle 超时，模型 sleep 只等外部事件唤醒，
    # 取消「无事件时周期性 tick 自驱」。运行时可通过管理 API 热切换。
    passive_mode: bool = False

    code_hard_timeout: int = 300
    image_max_dimension: int = 960
    message_time_prefix: bool = True
    bubble_thinking: bool = True
    bubble_max_concurrent: int = Field(default=5, gt=0)
    # 多会话并发提示：滑动窗口内未接管会话数上穿阈值时，向模型注入泡泡并行提示。
    concurrency_hint_window_seconds: float = Field(default=180.0, gt=0)
    concurrency_hint_threshold: int = Field(default=2, ge=2)
    concurrency_hint_cooldown_seconds: float = Field(default=600.0, gt=0)
    # participant_id 整串匹配这些 glob 时，向对方显式说明泡泡转交并标识回复。
    # 环境变量传 JSON 数组；不含通配符的条目表示精确匹配，[] 可关闭全部默认匹配。
    bubble_handoff_transparency_participant_matches: list[str] = Field(
        default_factory=lambda: list(DEFAULT_BUBBLE_HANDOFF_TRANSPARENCY_PARTICIPANT_MATCHES)
    )
    # 通用 WebSocket/SSE 默认开启透明转交；空数组可显式关闭传输层匹配。
    bubble_handoff_transparency_stream_transports: list[Literal["websocket", "sse"]] = Field(
        default_factory=lambda: list(DEFAULT_BUBBLE_HANDOFF_TRANSPARENCY_STREAM_TRANSPORTS)
    )
    # 超时泡泡可在该窗口内通过 bubble_spawn(bubble_id=...) 接着执行；0 表示禁用续跑。
    bubble_timeout_resume_seconds: int = Field(default=300, ge=0)

    subconscious_thinking: bool = True
    subconscious_summarize_before_compress: bool = True
    subconscious_max_cycles: int = 5


class ChannelAccessRuleConfig(BaseModel):
    """Inbound and outbound participant matching rules for one Channel."""

    model_config = ConfigDict(extra="forbid")

    inbound_allow: list[str] = Field(default_factory=list)
    inbound_deny: list[str] = Field(default_factory=list)
    outbound_allow: list[str] = Field(default_factory=list)
    outbound_deny: list[str] = Field(default_factory=list)

    @field_validator(
        "inbound_allow",
        "inbound_deny",
        "outbound_allow",
        "outbound_deny",
        mode="before",
    )
    @classmethod
    def normalize_patterns(cls, value: object) -> object:
        if value is None:
            return []
        if not isinstance(value, list):
            return value
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError(tr("config.channel_access.patterns_must_be_strings"))
            pattern = item.strip()
            if pattern and pattern not in normalized:
                normalized.append(pattern)
        return normalized


class ChannelAccessConfig(RootModel[dict[str, ChannelAccessRuleConfig]]):
    """Channel name to participant access rules.

    A root model keeps the persisted shape compact::

        {"wecom": {"inbound_allow": ["wecom:*"]}}
    """

    root: dict[str, ChannelAccessRuleConfig] = Field(default_factory=dict)

    @field_validator("root", mode="before")
    @classmethod
    def normalize_channel_names(cls, value: object) -> object:
        if value is None:
            return {}
        if not isinstance(value, dict):
            return value
        normalized: dict[str, object] = {}
        for raw_name, rules in value.items():
            if not isinstance(raw_name, str):
                raise ValueError(tr("config.channel_access.names_must_be_strings"))
            name = raw_name.strip()
            if not name:
                raise ValueError(tr("config.channel_access.names_must_not_be_empty"))
            if name in normalized:
                raise ValueError(
                    tr("config.channel_access.duplicate_name", channel=name)
                )
            normalized[name] = rules
        return normalized


class WeComBotConfig(BaseModel):
    """Configuration for one independently connected WeCom (企业微信) bot identity."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    bot_id: str = ""
    secret: str = Field(default="", repr=False)
    ws_url: str = ""

    @field_validator("bot_id")
    @classmethod
    def _strip_bot_id(cls, value: str) -> str:
        return value.strip()

    @field_validator("ws_url")
    @classmethod
    def _strip_ws_url(cls, value: str) -> str:
        return value.strip()


class WeComConfig(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="WECOM__", env_file=".env", extra="ignore")

    # 兼容旧版单实例扁平写法（WECOM__ENABLED / BOT_ID / SECRET / WS_URL）。
    # 当没有显式 bots 时，这些扁平字段被折叠成一个名为 "default" 的实例，
    # 以便老配置无需修改即可继续使用；只有 bots 时才使用新结构。
    enabled: bool = False
    bot_id: str = ""
    secret: str = Field(default="", repr=False)
    ws_url: str = ""
    bots: dict[str, WeComBotConfig] = Field(default_factory=dict)

    @field_validator("bots", mode="before")
    @classmethod
    def _normalize_bot_ids(cls, value: object) -> object:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(tr("config.wecom.bots_must_be_object"))
        for instance_id in value:
            if not isinstance(instance_id, str) or not re.fullmatch(
                r"[a-z][a-z0-9_-]{0,31}", instance_id
            ):
                raise ValueError(
                    tr("config.wecom.instance_id_invalid", instance=instance_id)
                )
        return value

    @model_validator(mode="after")
    def _fold_legacy_singleton(self) -> WeComConfig:
        if self.bots:
            legacy_given = bool(self.bot_id or self.secret or self.ws_url)
            if not legacy_given:
                return self
            # pydantic-settings 在把 WeComConfig 作为 Config 的嵌套字段读取时，
            # 会对已经折叠过一次的实例再做一次校验；如果只有一个与扁平字段
            # 等价的 default 实例，保留扁平字段以便后续 admin 覆盖仍能识别它。
            if len(self.bots) == 1 and set(self.bots) == {"default"}:
                default_bot = self.bots["default"]
                if (
                    default_bot.enabled == self.enabled
                    and default_bot.bot_id == self.bot_id
                    and default_bot.secret == self.secret
                    and default_bot.ws_url == self.ws_url
                ):
                    return self
            # 显式 bots 优先。旧式扁平字段只是 default 实例的旧写法：先把扁平
            # 值并入显式列出的 default（条目自身字段优先），避免只有扁平层保存
            # 的 secret 在清空扁平字段时丢失；但绝不删除 bots 里显式列出的
            # default，它可能是管理员正在使用的原始 Bot（例如在旧版扁平配置
            # 基础上新增第二个实例）。被折叠出的 default 是否要在层合并时移除，
            # 由 merge_config_layers 依据管理端是否显式列出 default 来决定，
            # 见 _reconcile_wecom_layers。
            explicit_default = self.bots.get("default")
            if explicit_default is not None:
                flat = {
                    "enabled": self.enabled,
                    "bot_id": self.bot_id,
                    "secret": self.secret,
                    "ws_url": self.ws_url,
                }
                self.bots["default"] = WeComBotConfig.model_validate(
                    _merge_legacy_default(flat, explicit_default.model_dump(mode="json"))
                )
            self.bot_id = ""
            self.secret = ""
            self.ws_url = ""
            return self
        # 没有显式实例时，把旧版扁平字段折叠成一个默认实例，保持老配置可用。
        # 这里不清理扁平字段：后续 admin 覆盖若添加显式 bots，仍能据此识别并
        # 替换这个自动生成的 default，而不是新旧两套实例同时运行。
        if self.bot_id or self.secret or self.ws_url or self.enabled:
            self.bots = {
                "default": WeComBotConfig(
                    enabled=self.enabled,
                    bot_id=self.bot_id,
                    secret=self.secret,
                    ws_url=self.ws_url,
                )
            }
        return self


class WeixinConfig(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="WEIXIN__", env_file=".env", extra="ignore")

    enabled: bool = True


class TelegramBotConfig(BaseModel):
    """Configuration for one independently polled Telegram Bot identity."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    display_name: str = ""
    bot_token: str = Field(default="", repr=False)
    api_base_url: str = "https://api.telegram.org"
    local_mode: bool = False
    poll_timeout_seconds: float = Field(default=30.0, ge=1.0, le=50.0)

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 80:
            raise ValueError(tr("config.telegram.display_name_too_long"))
        return value

    @field_validator("bot_token")
    @classmethod
    def _normalize_bot_token(cls, value: str) -> str:
        return value.strip()

    @field_validator("api_base_url")
    @classmethod
    def _validate_api_base_url(cls, value: str) -> str:
        return _normalize_source_base_url(
            value,
            field_name="api_base_url",
            allow_empty=False,
        )


class TelegramConfig(_EnvSettings):
    model_config = SettingsConfigDict(
        env_prefix="TELEGRAM__",
        env_file=".env",
        extra="ignore",
    )

    bots: dict[str, TelegramBotConfig] = Field(default_factory=dict)

    @field_validator("bots", mode="before")
    @classmethod
    def _validate_instance_ids(cls, value: object) -> object:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(tr("config.telegram.bots_must_be_object"))
        for instance_id in value:
            if not isinstance(instance_id, str) or not re.fullmatch(
                r"[a-z][a-z0-9_-]{0,31}", instance_id
            ):
                raise ValueError(
                    tr("config.telegram.instance_id_invalid", instance=instance_id)
                )
        return value


class Config(_EnvSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm: LLMConfig = Field(default_factory=lambda: LLMConfig(max_tokens=DEFAULT_LLM_MAX_TOKENS))
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    relay: RelayConfig = Field(default_factory=RelayConfig)
    desktop_updates: DesktopUpdatesConfig = Field(default_factory=DesktopUpdatesConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    i18n: I18NConfig = Field(default_factory=I18NConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    channel_access: ChannelAccessConfig = Field(default_factory=ChannelAccessConfig)
    wecom: WeComConfig = Field(default_factory=WeComConfig)
    weixin: WeixinConfig = Field(default_factory=WeixinConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


_LEGACY_WECOM_FLAT_FIELDS = ("enabled", "bot_id", "secret", "ws_url")


def _merge_legacy_default(flat: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Fold legacy flat fields into an explicit ``default`` bot entry.

    Non-empty entry fields win; empty (``""``) entry fields fall back to the
    flat values, because the admin UI cannot distinguish "left unchanged" (a
    masked secret / untouched input) from "cleared to empty", and the flat
    layer may be the only remaining copy of values such as the secret.
    """
    merged = dict(flat)
    for key, value in entry.items():
        if key in _LEGACY_WECOM_FLAT_FIELDS and value in ("", None):
            continue
        merged[key] = value
    return merged


def _reconcile_wecom_layers(
    merged: dict[str, Any],
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the legacy flat-field / explicit-bots ambiguity after a layer merge.

    ``merged`` is the deep-merged ``wecom`` section of a base config and an
    admin override layer; ``overrides`` is that override layer's ``wecom``
    section. The legacy flat fields (``WECOM__BOT_ID`` etc.) are sugar for the
    ``default`` instance. When the admin layer manages an explicit ``bots``
    map:

    - the flat fields are superseded: they are folded into the merged
      ``default`` entry (see :func:`_merge_legacy_default`) so a secret that
      only exists in the flat layer -- e.g. written by the pre-multi-bot admin
      console as ``wecom.secret`` -- is not destroyed;
    - a ``default`` instance that exists only because the base layer folded
      those flat fields is removed as well -- unless the admin layer itself
      lists ``default``, in which case the administrator explicitly owns that
      instance and it must survive (e.g. adding a second bot to a deployment
      that started from legacy flat config must not kill the original bot).
    """
    if not isinstance(merged, dict):
        return merged
    merged_bots = merged.get("bots")
    override_bots = overrides.get("bots") if isinstance(overrides, dict) else None
    if not isinstance(merged_bots, dict) or not isinstance(override_bots, dict):
        return merged
    flat = {
        key: merged.pop(key)
        for key in _LEGACY_WECOM_FLAT_FIELDS
        if key in merged
    }
    if "default" not in override_bots:
        merged_bots.pop("default", None)
    elif flat:
        default_entry = merged_bots.get("default")
        if isinstance(default_entry, dict):
            merged_bots["default"] = _merge_legacy_default(flat, default_entry)
    return merged


def merge_config_layers(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge a base config dict with an admin override layer.

    The ``wecom`` section gets extra reconciliation (see
    :func:`_reconcile_wecom_layers`) so legacy flat fields never silently
    delete an explicitly managed ``default`` bot instance.
    """
    merged = _deep_merge(base, overrides)
    if isinstance(merged.get("wecom"), dict) and isinstance(overrides.get("wecom"), dict):
        merged["wecom"] = _reconcile_wecom_layers(merged["wecom"], overrides["wecom"])
    return merged


def load_admin_overrides(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        return {}
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"读取管理配置 {source} 失败：{e}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"管理配置 {source} 顶层必须是 JSON 对象")
    return _evolve_admin_default_overrides(raw)


def _evolve_admin_default_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    agent = overrides.get("agent")
    if not isinstance(agent, dict):
        return overrides
    matches = agent.get(_HANDOFF_MATCHES_KEY)
    if not isinstance(matches, list) or tuple(matches) not in _LEGACY_HANDOFF_DEFAULTS:
        return overrides
    evolved = dict(overrides)
    evolved_agent = dict(agent)
    evolved_agent[_HANDOFF_MATCHES_KEY] = list(
        DEFAULT_BUBBLE_HANDOFF_TRANSPARENCY_PARTICIPANT_MATCHES
    )
    evolved["agent"] = evolved_agent
    return evolved


# Bot maps are managed as a whole by the admin UI: it always submits the full
# list, so per-key diffing against the inherited layer would erase entries whose
# secret was masked (a bot that otherwise matches the inherited config would be
# treated as fully inherited and dropped, making the persisted list incomplete).
_BOT_MAP_PATHS = frozenset(
    {"wecom.bots", "telegram.bots", "api.communication_tokens"}
)


def _remove_inherited_values(
    overrides: dict[str, Any],
    inherited: dict[str, Any],
    prefix: str = "",
) -> dict[str, Any]:
    sparse: dict[str, Any] = {}
    for key, value in overrides.items():
        path = f"{prefix}.{key}" if prefix else key
        inherited_value = inherited.get(key)
        if (
            isinstance(value, dict)
            and isinstance(inherited_value, dict)
            and path not in _BOT_MAP_PATHS
        ):
            nested = _remove_inherited_values(value, inherited_value, prefix=path)
            if nested:
                sparse[key] = nested
        elif key not in inherited or value != inherited_value:
            sparse[key] = value
    return sparse


def sparse_admin_overrides(
    overrides: dict[str, Any],
    inherited: Config,
) -> dict[str, Any]:
    return _remove_inherited_values(overrides, inherited.model_dump(mode="json"))


def write_admin_overrides(path: str | Path, overrides: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(overrides, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    if os.name != "nt":
        destination.chmod(0o600)


def normalize_admin_overrides_file(inherited: Config) -> bool:
    path = Path(inherited.admin.config_file)
    overrides = load_admin_overrides(path)
    sparse = sparse_admin_overrides(overrides, inherited)
    if sparse == overrides:
        return False
    write_admin_overrides(path, sparse)
    return True


def apply_admin_config_file(config: Config) -> Config:
    """以最高静态优先级应用管理页持久化的 typed JSON 覆盖。"""

    overrides = load_admin_overrides(config.admin.config_file)
    if not overrides:
        return config
    merged = merge_config_layers(config.model_dump(), overrides)
    # config_file 的位置由启动环境决定，禁止覆盖文件把自身重定向到其他路径。
    merged.setdefault("admin", {})["config_file"] = config.admin.config_file
    return Config.model_validate(merged)


def effective_admin_token(config: Config) -> str:
    """Return the token accepted by the management API."""

    return config.admin.token or config.desktop_updates.admin_token


def effective_communication_token(config: Config) -> str:
    """Return the token accepted by Desktop communication endpoints."""

    return config.api.communication_token or effective_admin_token(config)


def ensure_admin_token(config: Config) -> str | None:
    """Create and persist a first-run admin token when none was configured.

    Returns the generated token so the caller can show it once on the console.
    Existing ``ADMIN__TOKEN`` and desktop-update tokens are never changed.
    """

    if effective_admin_token(config):
        return None

    token = secrets.token_urlsafe(24)
    path = Path(config.admin.config_file)
    overrides = load_admin_overrides(path)
    updated = _deep_merge(overrides, {"admin": {"token": token}})
    write_admin_overrides(path, updated)
    config.admin.token = token
    return token
