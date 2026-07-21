import json
import os
import secrets
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from coworker.core.constants import (
    DEFAULT_BUBBLE_HANDOFF_TRANSPARENCY_PARTICIPANT_MATCHES,
    DEFAULT_BUBBLE_HANDOFF_TRANSPARENCY_STREAM_TRANSPORTS,
    DEFAULT_LLM_MAX_TOKENS,
)
from coworker.i18n import SupportedLocale, normalize_locale

# 扁平字段（LLM__<TYPE>_API_KEY / _BASE_URL）支持的内置 provider 类型，
# 用于把老式扁平配置自动展开成 name==type 的默认命名实例。
_FLAT_PROVIDER_TYPES = ("anthropic", "openai", "deepseek", "qwen", "zhipu", "minimax")


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
    summary_provider: str = ""
    summary_model: str = ""
    summary_thinking: bool = False

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

    # 独立的命名 provider 列表文件（JSON 数组，每项 {name,type,api_key,base_url,default_model?}）。
    # 文件不存在则忽略；其条目按 name 覆盖/扩展上面的扁平默认实例，支持同类型多实例。
    providers_file: str = "providers.json"
    runtime_config_file: str = "data/model_runtime_config.json"
    # 管理控制台维护的命名实例；由 admin_config.json 持久化，按 name 覆盖其他来源。
    managed_providers: list[ProviderSpec] = Field(default_factory=list)

    vision_provider: str = ""
    vision_model: str = ""
    # 保持历史视觉分析默认启用 thinking；可设为 false 以降低延迟和成本。
    vision_thinking: bool = True

    def resolved_providers(self) -> list[ProviderSpec]:
        """合并「扁平字段展开的默认实例」与「providers_file 中的命名实例」。

        - 扁平字段：每个非空 <type>_api_key 产出一个 name==type 的默认实例。
        - 文件条目：按 name 覆盖同名默认、或新增命名实例（如多个智谱）。
        返回按插入顺序去重后的规格列表。type 是否受支持留给工厂校验。
        """
        specs: dict[str, ProviderSpec] = {}
        for type_ in _FLAT_PROVIDER_TYPES:
            api_key = getattr(self, f"{type_}_api_key", "")
            if api_key:
                specs[type_] = ProviderSpec(
                    name=type_,
                    type=type_,
                    api_key=api_key,
                    base_url=getattr(self, f"{type_}_base_url", ""),
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


class MemoryConfig(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="MEMORY__", env_file=".env", extra="ignore")

    db_path: str = "data/memory"
    short_term_max_tokens: int = 80_000
    compress_threshold: float = 0.55
    compress_ratio: float = 0.25
    compress_protected_tail: float = (
        0.40  # legacy 单锚点路径用；tree 启用时由 tree_tail_fraction 取代
    )

    # 多分辨率记忆块树（Memory Block Tree）。tree_enabled=False 回退到旧的单锚点压缩。
    tree_enabled: bool = True
    tree_tail_fraction: float = 0.70  # 尾部保留原始消息的 token 占比
    tree_spine_cap_fraction: float = (
        0.30  # 脊柱 token 上限占比；唯一预算旋钮，节点预算/K 均由它导出
    )
    tree_backfill_max_leaves: int = 64  # `--backfill-tree` 一次性回溯生成叶子数上限
    tree_backfill_concurrency: int = 5  # 回溯时叶子摘要/归约合并的并发上限
    tree_merge_reach_depth: int = 2  # 高层合并向下够细层数：2=低两层、1=仅直接子摘要
    log_rotation_enabled: bool = False  # 日志物理轮转（后续项）；寻址层已抗分片

    auto_recall_enabled: bool = True
    auto_recall_relevance_threshold: float = 0.5
    auto_recall_limit: int = 5

    recent_activity_enabled: bool = True
    recent_activity_days: int = 7
    recent_activity_chunk_tokens: int = 120
    recent_activity_overlap_tokens: int = 24
    recent_activity_query_limit: int = 5
    recent_activity_auto_recall_enabled: bool = True
    recent_activity_auto_recall_limit: int = 2
    recent_activity_auto_recall_relevance_threshold: float = 0.72

    mem0_llm_provider: str = "deepseek"
    mem0_llm_model: str = "deepseek-v4-flash"
    mem0_embedder_model: str = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"


class APIConfig(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="API__", env_file=".env", extra="ignore")

    # Bind to loopback by default.  Public deployments should put an explicit
    # reverse proxy/TLS boundary in front of the API instead of exposing the
    # development server directly.
    host: str = "127.0.0.1"
    port: int = 8000
    communication_token: str = ""
    development_mode: bool = False
    # JSON list in environment/.env, e.g.
    # ["https://desktop.example", "http://127.0.0.1:1420"]
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ]
    )


class DesktopUpdatesConfig(_EnvSettings):
    model_config = SettingsConfigDict(
        env_prefix="DESKTOP_UPDATES__",
        env_file=".env",
        extra="ignore",
    )

    dir: str = "data/desktop_updates"
    admin_token: str = ""


class AdminConfig(_EnvSettings):
    """管理控制台配置。

    ``token`` 只从启动配置读取；管理页永远不会回显它。``config_file`` 是管理页
    保存的托管覆盖层，优先级高于 .env，但低于模型运行态热更新文件。
    """

    model_config = SettingsConfigDict(env_prefix="ADMIN__", env_file=".env", extra="ignore")

    token: str = ""
    config_file: str = "data/admin_config.json"


class I18NConfig(_EnvSettings):
    """Instance-wide runtime locale, independent from Web/Desktop UI language."""

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
    interaction_log_rotation_bytes: int = 10 * 1024 * 1024
    skills_dir: str = ".coworker/skills"
    palaces_dir: str = ".coworker/palaces"
    subconscious_dir: str = ".coworker/subconscious"

    idle_sleep_seconds: int = 30
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
    bubble_max_concurrent: int = 5
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


class WeComConfig(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="WECOM__", env_file=".env", extra="ignore")

    enabled: bool = False
    bot_id: str = ""
    secret: str = ""
    ws_url: str = ""


class Config(_EnvSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm: LLMConfig = Field(default_factory=lambda: LLMConfig(max_tokens=DEFAULT_LLM_MAX_TOKENS))
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    desktop_updates: DesktopUpdatesConfig = Field(default_factory=DesktopUpdatesConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    i18n: I18NConfig = Field(default_factory=I18NConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    wecom: WeComConfig = Field(default_factory=WeComConfig)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
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
    return raw


def apply_admin_config_file(config: Config) -> Config:
    """以最高静态优先级应用管理页持久化的 typed JSON 覆盖。"""

    overrides = load_admin_overrides(config.admin.config_file)
    if not overrides:
        return config
    merged = _deep_merge(config.model_dump(), overrides)
    # config_file 的位置由启动环境决定，禁止覆盖文件把自身重定向到其他路径。
    merged.setdefault("admin", {})["config_file"] = config.admin.config_file
    return Config.model_validate(merged)


def ensure_admin_token(config: Config) -> str | None:
    """Create and persist a first-run admin token when none was configured.

    Returns the generated token so the caller can show it once on the console.
    Existing ``ADMIN__TOKEN`` and desktop-update tokens are never changed.
    """

    if config.admin.token or config.desktop_updates.admin_token:
        return None

    token = secrets.token_urlsafe(24)
    path = Path(config.admin.config_file)
    overrides = load_admin_overrides(path)
    updated = _deep_merge(overrides, {"admin": {"token": token}})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    if os.name != "nt":
        path.chmod(0o600)
    config.admin.token = token
    return token
