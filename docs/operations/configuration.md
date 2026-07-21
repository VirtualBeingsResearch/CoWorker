# 配置与模型

中文 · [English](configuration.en.md)

[← 返回配置与运维](README.md)

## 基础配置

当前只支持从源码 checkout 运行。常用配置可以通过首次初始化向导、
`.env` 或环境变量提供，变量名使用双下划线分组；需要无人值守配置时可从
仓库根目录的 `.env.example` 开始。

配置优先级为：管理端保存的 `data/admin_config.json` 高于 `.env`，`.env` 高于
操作系统环境变量。`data/model_runtime_config.json` 只覆盖在线修改的 summary、
fallbacks 和 vision 设置。容器或服务管理器注入环境变量时，请确认工作目录中
没有同名 `.env` 配置。

### LLM

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM__DEFAULT_PROVIDER` | `deepseek` | 默认 LLM Provider |
| `LLM__DEFAULT_MODEL` | `deepseek-v4-pro` | 默认模型 |
| `LLM__MAX_TOKENS` | `8192` | 单次 LLM 响应的最大输出 token 数 |
| `LLM__SUMMARY_PROVIDER` | 空 | 摘要/压缩专用 provider；留空则沿用当前主线 provider |
| `LLM__SUMMARY_MODEL` | 空 | 摘要/压缩专用模型；只填它会复用当前 provider，留空且已配置 `SUMMARY_PROVIDER` 时使用该 provider 的 `default_model` |
| `LLM__SUMMARY_THINKING` | `false` | 摘要/压缩调用是否启用 thinking，默认关闭以降低延迟和成本 |
| `LLM__FALLBACKS` | `[]` | 主模型失败后的有序降级链，使用 JSON 数组，每项为 `providerName` 或 `providerName/modelId` |
| `LLM__ANTHROPIC_API_KEY` | 空 | Anthropic API Key |
| `LLM__ANTHROPIC_BASE_URL` | 空 | Anthropic 自定义 Base URL |
| `LLM__OPENAI_API_KEY` | 空 | OpenAI API Key |
| `LLM__OPENAI_BASE_URL` | 空 | OpenAI 自定义 Base URL |
| `LLM__DEEPSEEK_API_KEY` | 空 | DeepSeek API Key |
| `LLM__DEEPSEEK_BASE_URL` | 空（未配置时使用 `https://api.deepseek.com`） | DeepSeek 自定义 Base URL |
| `LLM__QWEN_API_KEY` | 空 | Qwen / DashScope API Key |
| `LLM__QWEN_BASE_URL` | 空（未配置时使用 DashScope 兼容模式地址） | Qwen 自定义 Base URL |
| `LLM__ZHIPU_API_KEY` | 空 | 智谱 API Key |
| `LLM__ZHIPU_BASE_URL` | 空（未配置时使用智谱 OpenAI 兼容地址） | 智谱自定义 Base URL |
| `LLM__MINIMAX_API_KEY` | 空 | MiniMax API Key |
| `LLM__MINIMAX_BASE_URL` | 空（未配置时使用 MiniMax OpenAI 兼容地址） | MiniMax 自定义 Base URL |
| `LLM__PROVIDERS_FILE` | `providers.json` | 命名 Provider 列表文件（见下方「多实例 Provider」）；文件不存在则忽略 |
| `LLM__RUNTIME_CONFIG_FILE` | `data/model_runtime_config.json` | 在线修改 summary / fallbacks / vision 后写入的运行态覆盖文件；启动时覆盖 `.env` 中同名模型配置 |
| `LLM__VISION_PROVIDER` | 空 | 视觉分析工具使用的 provider；留空时 `visual_analyze` 会提示先配置 |
| `LLM__VISION_MODEL` | 空 | 视觉分析工具使用的模型；分析视频时还需 Provider 声明原生视频能力 |
| `LLM__VISION_THINKING` | `true` | 视觉分析调用是否启用 thinking；设为 `false` 可使用支持的 Provider 的非思考模式，降低延迟和成本 |

### 记忆

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MEMORY__DB_PATH` | `data/memory` | 长期记忆数据库目录 |
| `MEMORY__SHORT_TERM_MAX_TOKENS` | `80000` | 短期记忆 token 上限 |
| `MEMORY__COMPRESS_THRESHOLD` | `0.55` | 触发自动压缩的阈值 |
| `MEMORY__COMPRESS_RATIO` | `0.25` | legacy 单锚点压缩时每次处理的上下文比例 |
| `MEMORY__COMPRESS_PROTECTED_TAIL` | `0.40` | legacy 单锚点压缩时保留在尾部的原始消息比例 |
| `MEMORY__TREE_ENABLED` | `true` | 启用多分辨率记忆树（关闭则回退旧的单锚点压缩） |
| `MEMORY__TREE_TAIL_FRACTION` | `0.70` | 尾部保留原始消息的 token 占比 |
| `MEMORY__TREE_SPINE_CAP_FRACTION` | `0.30` | 记忆树脊柱 token 上限占比 |
| `MEMORY__TREE_BACKFILL_MAX_LEAVES` | `64` | `--backfill-tree` 一次性回溯历史生成的叶子数上限 |
| `MEMORY__TREE_BACKFILL_CONCURRENCY` | `5` | 回溯时叶子摘要/归约合并的并发上限 |
| `MEMORY__TREE_MERGE_REACH_DEPTH` | `2` | 高层合并向下读取的细节层数；`2` 表示低两层 |
| `MEMORY__AUTO_RECALL_ENABLED` | `true` | 是否在收到消息时自动检索长期记忆 |
| `MEMORY__AUTO_RECALL_RELEVANCE_THRESHOLD` | `0.5` | 自动回忆的相关度阈值（0-1） |
| `MEMORY__AUTO_RECALL_LIMIT` | `5` | 每次自动回忆最多注入条数 |
| `MEMORY__RECENT_ACTIVITY_ENABLED` | `true` | 是否维护最近活动语义索引 |
| `MEMORY__RECENT_ACTIVITY_DAYS` | `7` | 最近活动索引覆盖的天数 |
| `MEMORY__RECENT_ACTIVITY_CHUNK_TOKENS` | `120` | 超长工具结果的索引分块大小 |
| `MEMORY__RECENT_ACTIVITY_OVERLAP_TOKENS` | `24` | 相邻索引分块的重叠 token 数 |
| `MEMORY__RECENT_ACTIVITY_QUERY_LIMIT` | `5` | 最近活动查询最多返回条数 |
| `MEMORY__RECENT_ACTIVITY_AUTO_RECALL_ENABLED` | `true` | 自动回忆时是否同时查询最近活动 |
| `MEMORY__RECENT_ACTIVITY_AUTO_RECALL_LIMIT` | `2` | 自动回忆最多注入的最近活动条数 |
| `MEMORY__RECENT_ACTIVITY_AUTO_RECALL_RELEVANCE_THRESHOLD` | `0.72` | 最近活动自动回忆的相关度阈值 |
| `MEMORY__MEM0_LLM_PROVIDER` | `deepseek` | mem0 内部记忆提取使用的 LLM provider |
| `MEMORY__MEM0_LLM_MODEL` | `deepseek-v4-flash` | mem0 内部记忆提取使用的模型 |
| `MEMORY__MEM0_EMBEDDER_MODEL` | `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` | mem0 与最近活动索引使用的嵌入模型；已有数据不应直接切换模型 |

### Agent

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT__INBOX_DIR` | `data/inbox` | 文件消息输入目录 |
| `AGENT__OUTBOX_DIR` | `data/outbox` | 文件消息输出目录 |
| `AGENT__IDENTITY_DIR` | `data/identity` | 身份文件目录 |
| `AGENT__LOGS_DIR` | `data/logs` | 日志目录 |
| `AGENT__INTERACTION_LOG_ROTATION_BYTES` | `10485760` | 单个交互日志分片的最大字节数；达到阈值后当前 `interactions.jsonl` 会归档为递增编号分片并继续写入新文件。设为 `0` 可关闭轮转。 |
| `AGENT__IDLE_SLEEP_SECONDS` | `30` | 空闲休眠秒数 |
| `AGENT__INBOX_POLL_INTERVAL` | `2.0` | inbox 轮询间隔 |
| `AGENT__TICK` | `true` | 是否启用无外部消息时的自主 tick |
| `AGENT__CODE_HARD_TIMEOUT` | `300` | 代码执行工具硬超时秒数 |
| `AGENT__IMAGE_MAX_DIMENSION` | `960` | 图片发送给模型前的最大长边像素，超出则等比缩放 |
| `AGENT__MESSAGE_TIME_PREFIX` | `true` | 是否给发往模型的用户消息添加本地时间前缀 |
| `AGENT__BUBBLE_THINKING` | `true` | 是否启用泡泡并行思考 |
| `AGENT__BUBBLE_MAX_CONCURRENT` | `5` | 泡泡思考最大并发数 |
| `AGENT__BUBBLE_HANDOFF_TRANSPARENCY_PARTICIPANT_MATCHES` | `["wecom:*", "coworker-desktop:*:local:*"]` | JSON glob 数组，按大小写敏感的整串 `participant_id` 匹配；不含通配符的条目表示精确匹配。命中对象会收到带 Bubble ID 的接管、续跑和结束提示，Bubble 直接回复也会带结构化来源。默认匹配企微和 Desktop `local` actor；设为 `[]` 可关闭全部默认 participant 匹配。 |
| `AGENT__BUBBLE_HANDOFF_TRANSPARENCY_STREAM_TRANSPORTS` | `["websocket", "sse"]` | JSON 传输层数组，可填 `websocket`、`sse`；两者默认开启，因此在线通用长连接默认使用透明转交。任何未命中 participant glob 的 Desktop actor 都不会被此通用规则兜底命中，因此仍排除 `claude` 与 `codex`。设为 `[]` 可关闭传输层匹配。 |
| `AGENT__BUBBLE_TIMEOUT_RESUME_SECONDS` | `300` | 泡泡达到最大轮次后允许通过 `bubble_spawn(bubble_id=...)` 续跑的宽限期（秒）；设为 `0` 禁用续跑。 |
| `AGENT__SUBCONSCIOUS_THINKING` | `true` | 是否启用潜意识后台思考 |
| `AGENT__SUBCONSCIOUS_SUMMARIZE_BEFORE_COMPRESS` | `true` | 压缩前是否触发潜意识总结 |
| `AGENT__SUBCONSCIOUS_MAX_CYCLES` | `5` | 单次潜意识任务最大 cycle 数 |

### API、管理端与通信

| 变量 | 默认值 | 说明 |
|---|---|---|
| `API__HOST` | `127.0.0.1` | API 监听地址；如需对外提供服务，应由显式配置的反向代理/TLS 层接入 |
| `API__PORT` | `8000` | API 监听端口 |
| `API__CORS_ORIGINS` | `["http://localhost:8000", "http://127.0.0.1:8000"]` | 允许访问 API 的浏览器来源 JSON 列表；空列表关闭跨域请求 |
| `API__DEVELOPMENT_MODE` | `false` | Desktop 开发模式；关闭 Bearer/HTTPS 校验，仅应为本机 HTTP 调试显式开启 |
| `API__COMMUNICATION_TOKEN` | 空（必填） | Desktop 生产通信 Bearer 令牌；`API__DEVELOPMENT_MODE=false` 时必须配置 |
| `ADMIN__TOKEN` | 首次启动自动生成 | `/admin` 管理控制台和 `/api/admin/*` 的 Bearer 令牌；自动值会保存到管理端配置文件 |
| `ADMIN__CONFIG_FILE` | `data/admin_config.json` | 管理页保存的 typed JSON 覆盖层，优先级高于 `.env`；非热更新配置重启后生效 |
| `DESKTOP_UPDATES__DIR` | `data/desktop_updates` | Desktop 自动更新 release 与 asset 的存储目录 |
| `DESKTOP_UPDATES__ADMIN_TOKEN` | 空 | Desktop 更新管理 API 的 Bearer 令牌 |
| `WECOM__ENABLED` | `false` | 是否启用企业微信智能机器人长连接 |
| `WECOM__BOT_ID` | 空 | 企业微信机器人 ID |
| `WECOM__SECRET` | 空 | 企业微信机器人 Secret |
| `WECOM__WS_URL` | 空 | 可选的企业微信 WebSocket 地址；留空使用 SDK 默认地址 |

## 支持的模型

内置 Provider 类型为 `anthropic`、`openai`、`deepseek`、`qwen`、`zhipu` 和
`minimax`。Coworker 只允许切换到对应 Provider 标记为支持工具调用的模型；精确列表
会随代码更新，以首次初始化向导和 [`src/coworker/brain/`](../../src/coworker/brain/)
中的 Provider 实现为准，避免文档复制一份快速过期的模型清单。

只有在对应 API Key 存在时，该 Provider 才会被注册。`LLM__DEFAULT_PROVIDER`
必须指向已注册的 Provider 实例名。

### 多实例 Provider（providers.json）

上面的扁平字段（`LLM__ZHIPU_API_KEY` 等）每种类型只能配一份。若需要**同一类型的多个实例**（例如多个智谱 Key 面向不同用户），在 `LLM__PROVIDERS_FILE` 指向的 JSON 文件里按 `name` 列举即可。每个 Provider 的「类型（API 方言/模型表）」与「注册名（注册表 key、`default_provider`/`switch_model` 引用的名字）」由此解耦：

```json
[
  { "name": "zhipu-userA", "type": "zhipu", "api_key": "...", "default_model": "glm-5.1" },
  { "name": "zhipu-userB", "type": "zhipu", "api_key": "...", "base_url": "...", "default_model": "glm-4.7" }
]
```

字段：`name`（必填，注册名，需唯一）、`type`（必填，取上面的内置 Provider
类型之一）、`api_key`、`base_url`（可选）、`default_model`（可选，`switch_model`
切到该实例但不指定模型时使用）。

- 扁平字段仍然有效，会自动并入为 `name == type` 的默认实例；文件中的同名条目按 `name` 覆盖它。
- 文件不存在则忽略，老配置零改动照常运行。
- 完整示例见仓库根目录 `providers.json.example`。
