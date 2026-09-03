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
没有同名 `.env` 配置。管理端只向 `admin_config.json` 写入相对继承配置实际改变的
字段；保存时会移除已经恢复为 `.env` 或产品默认值的覆盖项。显式空列表若不同于继承值，
仍作为有效覆盖保留。因此未修改的默认字段会随版本演进，不会因打开或保存整个设置分组而
固定在旧值。启动时也会以原子写入自动规范化已有覆盖文件：旧快照中的继承值会被清理，
自定义值、密钥和显式覆盖保持不变。运行设置页会列出当前分组中的管理端覆盖，并允许将
单个字段恢复为继承配置；每个分组独立保留未保存草稿和密钥，只会提交当前分组。

未完成首次初始化时，Coworker 只启动管理 HTTP 服务，不启动 Agent 主循环、消息接收轮询或企业微信等外部通道。命令行每次启动都会显示当前有效的管理员令牌，浏览器访问 `/admin` 之外的页面或普通 API 会被引导到 `/admin`；管理页静态资源、登录校验和 bootstrap 接口仍正常放行。首次初始化向导会只读显示服务器当前时区，并检测浏览器时区来推荐相应的 `TZ` 环境变量，但快速体验不会自动修改服务器或容器时区；向导也可以设置运行时语言、单次输出 Token 上限，并从推荐目录选择或手动输入启动模型。保存后通过一次干净重启进入正常运行，不恢复首设阶段的短期快照，也不生成普通重启通知。

### 运行时语言与系统时区

| 变量 | 默认值 | 说明 |
|---|---|---|
| `I18N__LOCALE` | `zh-CN` | 实例级模型与运行时语言；支持 `zh-CN`、`en` 及常见别名（如 `zh`、`zh_CN`、`en-US`），规范化后重启生效 |
| `TZ` | 跟随操作系统；Docker Compose 默认为 `Asia/Shanghai` | 进程或容器的 IANA 时区（如 `Asia/Shanghai`）；Coworker 只读取该系统环境，不会在运行时修改 |

运行时 locale 独立于 Web/Desktop 界面语言，也可以在管理页「运行语言」中修改。
它控制 Coworker 自有的 system prompt、工具 schema/结果包装、摘要/记忆框架、Bubble、
潜意识、视觉请求、API 错误/响应说明、纳入 catalog 的运维警告与通知，以及参与者系统通知。Agent 对参与者的回复默认跟随当前消息语言；参与者
明确指定语言时优先；没有当前用户消息的自主输出回退到运行时 locale。切换 locale 不会
翻译用户内容、历史数据、第三方原文或已有 Identity/Skill/Palace/任务/记忆，因此新旧语言
混合属于兼容行为；重启检测到变化后会注入一条语言切换系统通知。

系统时区控制 system prompt 和 `get_context` 中的当前时间、消息时间前缀、闹钟对无偏移
时间的解释，以及任务等界面的日期边界。Coworker 没有独立的时区覆盖配置，管理员界面也
不会修改时区；请通过操作系统、容器或服务启动环境设置 `TZ`，再重启进程。首次初始化界面
只读显示进程当前时区作为对照，并通过 `Intl.DateTimeFormat` 检测浏览器的 IANA
时区，仅显示相应的 `TZ` 建议，不会写入配置。
反向代理不会改变检测结果，因为检测发生在管理员浏览器中，而不是代理或服务器上。

### LLM

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM__DEFAULT_PROVIDER` | `deepseek` | 默认 LLM Provider |
| `LLM__DEFAULT_MODEL` | `deepseek-v4-pro` | 默认模型 |
| `LLM__MAX_TOKENS` | `8192` | 单次 LLM 响应的最大输出 token 数 |
| `LLM__THINKING_EFFORT` | 空（Provider 默认） | 主线思考强度，取值 `none`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max`；由各 Provider 映射到原生档位 |
| `LLM__SUMMARY_PROVIDER` | 空 | 摘要/压缩专用 provider；留空则沿用当前主线 provider |
| `LLM__SUMMARY_MODEL` | 空 | 摘要/压缩专用模型；只填它会复用当前 provider，留空且已配置 `SUMMARY_PROVIDER` 时使用该 provider 的 `default_model` |
| `LLM__SUMMARY_THINKING` | `false` | 摘要/压缩调用是否启用 thinking，默认关闭以降低延迟和成本 |
| `LLM__SUMMARY_THINKING_EFFORT` | 空（Provider 默认） | 摘要/压缩思考强度，档位同 `LLM__THINKING_EFFORT` |
| `LLM__FALLBACKS` | `[]` | 主模型失败后的有序降级链，使用 JSON 数组，每项为 `providerName` 或 `providerName/modelId` |
| `LLM__MODEL_PRICES` | `[]` | 模型定价 JSON 数组，按 Provider 注册名和模型 ID 精确匹配；修改后热生效并按当前价格重算历史消费估算 |
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
| `LLM__OPENCODE_GO_API_KEY` | 空（未设置时兜底读取官方 `OPENCODE_API_KEY`） | OpenCode Go 订阅 API Key |
| `LLM__OPENCODE_GO_BASE_URL` | 空（未配置时使用 `https://opencode.ai/zen/go/v1`） | OpenCode Go 自定义 Base URL |
| `LLM__PROVIDERS_FILE` | `providers.json` | 命名 Provider 列表文件（见下方「多实例 Provider」）；文件不存在则忽略 |
| `LLM__RUNTIME_CONFIG_FILE` | `data/model_runtime_config.json` | 在线修改 thinking / summary / fallbacks / vision 后写入的运行态覆盖文件；启动时覆盖 `.env` 中同名模型配置 |
| `LLM__VISION_PROVIDER` | 空 | 视觉分析工具使用的 provider；留空时 `visual_analyze` 会提示先配置 |
| `LLM__VISION_MODEL` | 空 | 视觉分析工具使用的模型；分析视频时还需 Provider 声明原生视频能力 |
| `LLM__VISION_THINKING` | `true` | 视觉分析调用是否启用 thinking；设为 `false` 可使用支持的 Provider 的非思考模式，降低延迟和成本 |
| `LLM__VISION_THINKING_EFFORT` | 空（Provider 默认） | 视觉分析思考强度，档位同 `LLM__THINKING_EFFORT` |

`LLM__MODEL_PRICES` 的每项包含 `provider`、`model`、三个大写字母的 `currency`，以及
`input_per_million`、`output_per_million` 和可选的 `cached_input_per_million`。价格必须是
有限非负数；同一 Provider/模型只能出现一次。缓存输入价留空时使用普通输入价。例如：

```json
[
  {
    "provider": "openai",
    "model": "gpt-5.2",
    "currency": "USD",
    "input_per_million": 1.75,
    "output_per_million": 14,
    "cached_input_per_million": 0.175
  }
]
```

定价与连接来源相互独立，因此也能为 `.env` 或 `providers.json` 提供的只读连接补充价格。
管理后台保存定价不重建 Provider、无需重启；历史 Token 始终按当前价格实时重算。不同币种
分别汇总，不做汇率换算。管理后台会建议常用的 ISO 4217 三字母币种代码，同时保留其他
三字母代码的手动输入；金额符号由浏览器根据币种代码和界面语言生成，没有专用符号时显示
币种代码。

### 记忆

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MEMORY__DB_PATH` | `data/memory` | Coworker 记忆数据目录（短期快照、状态文件，以及默认 mem0 后端数据） |
| `MEMORY__BACKEND` | `mem0`（未设置 `MEMORY_DEFAULT_BACKEND` 时） | 长期记忆后端：`mem0`（默认）或 `file`（最简文件存储后端）。`mem0` 需安装可选依赖（`uv sync --extra mem0`）；`file` 为默认精简安装可用的后端。若使用 `mem0`，日常 `uv sync` / `uv run --sync` 也要带 `--extra mem0`，否则该 extra 会被从环境摘除 |
| `MEMORY_DEFAULT_BACKEND` | `mem0` | 未显式设置 `MEMORY__BACKEND` 时采用的默认记忆后端（`mem0` 或 `file`）。仅供如 `lite-offline` 之类精简镜像在构建时变更默认值，便于它们默认使用无需可选依赖的 `file` 后端；显式配置的 `MEMORY__BACKEND` 永远优先 |
| `MEMORY__SHORT_TERM_MAX_TOKENS` | `120000` | 最近一次完整模型输入达到该预算后触发一次短期记忆压缩；允许短暂超过 |
| `MEMORY__COMPRESS_RATIO` | `0.30` | 每次压缩处理当前 primary 中最旧消息的 token 比例；tree/legacy 共用 |
| `MEMORY__TREE_ENABLED` | `true` | 启用多分辨率记忆树（关闭则回退旧的单锚点压缩） |
| `MEMORY__TREE_SPINE_CAP_FRACTION` | `0.30` | 记忆树脊柱 token 上限占比 |
| `MEMORY__TREE_BACKFILL_MAX_LEAVES` | `64` | `--backfill-tree` 一次性回溯历史生成的叶子数上限 |
| `MEMORY__TREE_BACKFILL_CONCURRENCY` | `5` | 回溯时叶子摘要/归约合并的并发上限 |
| `MEMORY__TREE_MERGE_REACH_DEPTH` | `2` | 高层合并向下读取的细节层数；`2` 表示低两层 |
| `MEMORY__AUTO_RECALL_ENABLED` | `true` | 是否在收到消息时自动检索长期记忆 |
| `MEMORY__AUTO_RECALL_RELEVANCE_THRESHOLD` | `0.5` | mem0 查询结果的最低相关度分数（0-1）；自动回忆、`query_memory` 和管理端搜索统一使用，修改后热生效 |
| `MEMORY__AUTO_RECALL_LIMIT` | `5` | 每次自动回忆最多注入条数 |
| `MEMORY__MEM0_LLM_PROVIDER` | `""`（跟随主线） | mem0 记忆提取的独立 provider；留空跟随运行态主线 provider（包括手动切换与失败降级），也可显式指定 Brain provider 名称或类型，复用匹配实例的凭据和有效 `base_url`。修改后热生效，无需重启 |
| `MEMORY__MEM0_LLM_MODEL` | `""`（跟随主线） | mem0 记忆提取的独立模型 ID；provider 也留空时跟随运行态主线模型。显式指定 provider 但留空模型时，使用该 provider 的 `default_model`（无则 `LLM__DEFAULT_MODEL`）。模型 ID 原样传给对应 API 方言；修改后热生效，无需重启 |
| `MEMORY__MEM0_LLM_THINKING` | `false` | mem0 抽取 LLM 的 thinking 开关；对已知思考模型注入对应参数（抽取是结构化 JSON 任务，默认关闭避免思考吞 token）。修改后热生效，无需重启 |
| `MEMORY__MEM0_EMBEDDER_MODEL` | `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` | mem0 使用的嵌入模型；已有数据不应直接切换模型 |

### Agent

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT__INBOX_DIR` | `data/inbox` | 文件消息输入目录 |
| `AGENT__OUTBOX_DIR` | `data/outbox` | 文件消息输出目录 |
| `AGENT__IDENTITY_DIR` | `data/identity` | 身份文件目录 |
| `AGENT__LOGS_DIR` | `data/logs` | 日志目录 |
| `AGENT__SYSTEM_PROMPT_TEMPLATE` | 空 | System Prompt 模板；空值或纯空白使用产品标准模板，最长 100,000 字符。保存管理端覆盖后需安全重启 |
| `AGENT__INTERACTION_LOG_ROTATION_BYTES` | `52428800` | 单个交互日志分片的最大字节数；达到阈值后当前 `interactions.jsonl` 会归档为递增编号分片并继续写入新文件。设为 `0` 可关闭轮转。 |
| `AGENT__IDLE_SLEEP_SECONDS` | `30` | 空闲休眠秒数 |
| `AGENT__INBOX_POLL_INTERVAL` | `2.0` | inbox 轮询间隔 |
| `AGENT__TICK` | `true` | 是否启用无外部消息时的自主 tick |
| `AGENT__PASSIVE_MODE` | `false` | 是否启用 Passive 模式；启用后首次启动和重启都保持休息，启动通知静默保留到下一次真实唤醒，不再按空闲超时自唤醒 |
| `AGENT__CODE_HARD_TIMEOUT` | `300` | 代码执行工具硬超时秒数 |
| `AGENT__IMAGE_MAX_DIMENSION` | `960` | 图片发送给模型前的最大长边像素，超出则等比缩放 |
| `AGENT__MESSAGE_TIME_PREFIX` | `true` | 是否给发往模型的用户消息添加本地时间前缀 |
| `AGENT__BUBBLE_THINKING` | `true` | 是否启用泡泡并行思考 |
| `AGENT__BUBBLE_MAX_CONCURRENT` | `5` | 泡泡思考最大并发数 |
| `AGENT__CONCURRENCY_HINT_WINDOW_SECONDS` | `180.0` | 多会话并发提示的滑动窗口时长（秒）；窗口内出现过来信的会话视为同时活跃 |
| `AGENT__CONCURRENCY_HINT_THRESHOLD` | `2` | 窗口内未被泡泡接管的会话数上穿该阈值时，向模型注入泡泡并行提示；最小为 2 |
| `AGENT__CONCURRENCY_HINT_COOLDOWN_SECONDS` | `600.0` | 两次并发提示之间的最小间隔（秒） |
| `AGENT__BUBBLE_HANDOFF_TRANSPARENCY_PARTICIPANT_MATCHES` | `["wecom:*", "weixin:*", "tg:*", "coworker-desktop:*:local:*"]` | JSON glob 数组，按大小写敏感的整串 `participant_id` 匹配；不含通配符的条目表示精确匹配。命中对象在 Bubble 首次真实收发时收到带 ID 的接管或续跑提示，直接回复带来源；只有已公告的接管才发送结束提示。默认匹配企微、微信 Claw、Telegram 和 Desktop `local` actor；设为 `[]` 可关闭全部默认 participant 匹配。 |
| `AGENT__BUBBLE_HANDOFF_TRANSPARENCY_STREAM_TRANSPORTS` | `["websocket", "sse"]` | JSON 传输层数组，可填 `websocket`、`sse`；两者默认开启，因此在线通用长连接默认使用透明转交。任何未命中 participant glob 的 Desktop actor 都不会被此通用规则兜底命中，因此仍排除 `claude` 与 `codex`。设为 `[]` 可关闭传输层匹配。 |
| `AGENT__BUBBLE_TIMEOUT_RESUME_SECONDS` | `300` | 泡泡达到最大轮次后允许通过 `bubble_spawn(bubble_id=...)` 续跑的宽限期（秒）；设为 `0` 禁用续跑。 |
| `AGENT__SUBCONSCIOUS_THINKING` | `true` | 是否启用潜意识后台思考 |
| `AGENT__SUBCONSCIOUS_SUMMARIZE_BEFORE_COMPRESS` | `true` | 压缩前是否触发潜意识总结 |
| `AGENT__SUBCONSCIOUS_MAX_CYCLES` | `5` | 单次潜意识任务最大 cycle 数 |

`AGENT__SYSTEM_PROMPT_TEMPLATE` 可以引用 `{{IDENTITY}}`、`{{ENVIRONMENT}}`、
`{{INSTINCTS}}`、`{{GUIDELINES}}`、`{{LANGUAGE_POLICY}}`、`{{THINKING}}`、
`{{CHANNELS}}`、`{{SKILLS}}` 和 `{{PALACES}}`。每个变量都包含区段标题和已渲染正文，
对应的 `{{IDENTITY_CONTENT}}`、`{{ENVIRONMENT_CONTENT}}` 等 `_CONTENT` 变量只包含正文，
可用于省略或自行定义 `[IDENTITY]` 之类的标题。变量必须独占一行且最多出现一次；同一区段的
完整变量和正文变量不能同时使用，未知、重复或冲突变量会导致配置校验失败。使用 `\{{NAME}}`
输出字面量占位符。变量可以重排或省略；模板不引用任何变量时会完全替换内置 Prompt。
方括号标题（包括 `[CUSTOM]`）只是普通正文，可自由改名、拆分或删除。工具 Schema 不属于模板，
仍由模型调用层提供。建议在管理页面“关系 → 身份档案”编辑多行模板；保存后通过安全重启生效。
编辑器提供同步行号和空白行计数；每个变量卡片还可预览当前运行实例渲染出的完整区段与正文。

### API、管理端与通信

| 变量 | 默认值 | 说明 |
|---|---|---|
| `API__HOST` | `127.0.0.1` | API 监听地址；如需对外提供服务，应由显式配置的反向代理/TLS 层接入 |
| `API__PORT` | `8000` | API 监听端口 |
| `API__PUBLIC_URL` | 空 | 反向代理后浏览器访问的公开 HTTP(S) 根地址，只能包含 scheme、host 和可选端口；首次初始化重连优先使用它，而不是内部监听地址 |
| `API__CORS_ORIGINS` | `["http://localhost:8000", "http://127.0.0.1:8000"]` | 允许访问 API 的浏览器来源 JSON 列表；空列表关闭跨域请求 |
| `API__COMMUNICATION_TOKEN` | 空（回退管理员令牌） | 生产通信 Bearer 令牌；显式设置后保护 Desktop 通信、普通 REST 消息、WebSocket/SSE 连接、运行日志流与 `/status` 完整快照。管理后台保存后立即生效，`.env` 修改需重启；需要与管理权限隔离时单独配置 |
| `API__COMMUNICATION_TOKENS` | `{}` | OpenAI 信道额外通信令牌：JSON 对象，短名 → 密钥。短名匹配 `[a-z][a-z0-9_-]{0,31}`；`api` 与 `control` 会被拒绝。签发路径是 `openai:control`；管理端可列出、复制、作废或新增。保存后立即刷新鉴权。Desktop 配对复制与 Relay 隧道身份仍使用主令牌；extras 可在直连与 Relay 内层 `/v1/*` 上鉴权 |
| `API__COMPAT_TIMEOUT_SECONDS` | `180` | OpenAI 兼容 `chat/completions` 等待 `communicate` 或客户端工具结果的超时秒数（1–3600） |
| `CHANNEL_ACCESS` | `{}` | 按信道设置 participant 入站/出站访问列表的 JSON 对象；每项可含 `inbound_allow`、`inbound_deny`、`outbound_allow`、`outbound_deny` |
| `ADMIN__TOKEN` | 首次启动自动生成 | `/admin` 管理控制台和 `/api/admin/*` 的 Bearer 令牌；自动值会保存到管理端配置文件 |
| `ADMIN__CONFIG_FILE` | `data/admin_config.json` | 管理页保存的 typed JSON 覆盖层，优先级高于 `.env`；非热更新配置重启后生效 |
| `DESKTOP_UPDATES__DIR` | `data/desktop_updates` | Desktop 自动更新 release 与 asset 的存储目录 |
| `DESKTOP_UPDATES__ADMIN_TOKEN` | 空 | Desktop 更新管理 API 的 Bearer 令牌 |
| `DESKTOP_UPDATES__SYNC_SOURCES` | `[]` | 上游来源 JSON 数组；每项都有稳定 `id`、`name` 和 `type`，支持多个 GitHub 仓库和多个 Coworker 实例 |
| `DESKTOP_UPDATES__SYNC_ACTIVE_SOURCE` | 空 | 当前活跃来源 UUID；空值表示关闭同步。只会运行一个活跃来源，不会并行同步多个来源 |
| `DESKTOP_UPDATES__FEED_TOKEN` | 空 | 允许其他 Coworker 实例同步本实例 published release feed 的独立 Token；空值表示 feed endpoint 关闭，不等同于管理员令牌 |
| `DESKTOP_UPDATES__SYNC_INTERVAL_SECONDS` | `21600` | 上游检测间隔，范围 300～604800 秒 |
| `DESKTOP_UPDATES__SYNC_ON_START` | `true` | 服务启动后是否立即检测一次 |
| `DESKTOP_UPDATES__SYNC_MAX_ASSET_BYTES` | `2147483648` | 单个制品允许下载的最大字节数 |
| `DESKTOP_UPDATES__SYNC_MAX_RUN_BYTES` | `4294967296` | 单次同步允许下载的最大总字节数 |
| `WECOM__BOTS` | `{}` | 按稳定 `instance_id` 配置多个企业微信 Bot 的 JSON 对象；每项支持 `enabled`、`bot_id`、`secret` 和 `ws_url`（`ws_url` 留空使用 SDK 默认地址）。仍兼容旧版扁平写法（`WECOM__ENABLED` / `BOT_ID` / `SECRET` / `WS_URL`，会自动归为 `default` 实例） |
| `TELEGRAM__BOTS` | `{}` | 按稳定 `instance_id` 配置多个 Telegram Bot 的 JSON 对象；每项支持 `enabled`、`display_name`、`bot_token`、`api_base_url`、`local_mode` 和 `poll_timeout_seconds` |
| `WEIXIN__ENABLED` | `true` | 是否启用个人微信 ClawBot 信道；无连接时不会产生网络轮询 |

反向代理同时代理 `/admin`、`/api/*` 和静态资源时，将 `API__PUBLIC_URL` 设置为浏览器
实际访问的 origin，例如 `https://coworker.example.com`。它不改变 `API__HOST` 或
`API__PORT` 的内部监听行为，只让首次初始化和重启后的管理员页面继续通过稳定的公开地址
连接；不要填写 `/admin`、路径、查询参数或凭据。如果前端与 API 使用不同 origin，仍需将
前端 origin 精确加入 `API__CORS_ORIGINS`。修改内部端口时，也必须在 Coworker 恢复前让
反向代理 upstream 指向新端口。

`CHANNEL_ACCESS` 的键是信道名，四类规则都是大小写敏感的整串 participant ID glob。deny
优先；allow 非空时只允许命中项；未配置或四个列表都为空时允许全部。该配置可在管理端
“信道访问”中热更新。内置键、拦截时机和示例见[信道访问列表](../channels/api-and-channels.md#信道访问列表)。

主循环处于休息状态时，管理端「生命总览」会显示「继续运行」。该操作本身只发送内部
唤醒信号，不创建新的 inbox 消息；模型从现有上下文继续，之前已静默排队的启动通知或
其他事件仍会按正常顺序处理。对应的管理员 API 是 `POST /api/admin/resume`，返回的
`resumed` 表示本次请求是否确实唤醒了休息中的主循环。

管理端保存企业微信配置后会立即启用、停用或重建 WebSocket 连接，不需要重启 Coworker。重连会清理仅属于旧连接的回复帧缓存，但保留已发现的联系人以及最近收发时间；若连接被企业微信判定为由新连接接替，运行时会等待下一次配置修改，而不会与新连接争抢重连。

Telegram 可以同时配置多个 Bot，并在管理端热新增、删除、启停或重建单个实例。每个实例
默认使用 `https://api.telegram.org`，也可以通过 `api_base_url` 指向代理或自托管机器人 API
服务器；仅当自托管服务器以 `--local` 启动并与 Coworker 共享文件路径时启用 `local_mode`。
participant ID 为
`tg:<instance_id>:<chat_id>`，token 在管理 API 中始终遮蔽。完整配置、Privacy Mode、附件
限制和排障见 [Telegram](../channels/telegram.md)。

微信 Claw 模块会同时注册 transport、管理接口和热设置应用器。扫码成功会把连接保存到
`MEMORY__DB_PATH/weixin_connections.json`，并立即启动一个
`weixin:<bot_instance_id>` participant；连接不是 `admin_config.json` 设置。一个 Bot 实例只能绑定一个微信账号。二维码查看者不会与该连接自动绑定，联系人关系仍由搭档组织。未结束的扫码会话在离开并返回管理页后可以恢复。详见[微信 Claw](../channels/weixin-claw.md)。

### 容器 Git 工作区

| 变量 | 默认值 | 说明 |
|---|---|---|
| `COWORKER_BUILD_TARGET` | `offline` | 使用 Compose 构建镜像时选择的 Dockerfile target：`offline`（默认）、`runtime`、`with-embedder` 或 `lite-offline` |
| `COWORKER_WITH_MEM0` | `true` | 使用 Compose 构建镜像时是否安装 mem0 可选依赖；构建 `lite-offline` target 时请设为 `false` |
| `COWORKER_BUNDLE_REPOSITORY_URL` | 官方 Coworker 仓库 | 构建镜像时转换为 Git bundle 的兼容仓库 |
| `COWORKER_BUNDLE_REPOSITORY_REF` | 仓库 `HEAD` | 构建时写入 bundle 元数据的分支、tag 或 commit |
| `COWORKER_WORKSPACE_PATH` | `/app` | 容器内实际运行源码与 Agent 共用的 Git 工作区 |
| `COWORKER_WORKSPACE_SOURCE` | `.` | Compose 的 `/app` 挂载源；默认使用当前 checkout，设为 `coworker-workspace` 可复用镜像托管的命名卷 |
| `COWORKER_STATE_PATH` | `/var/lib/coworker` | 持久运行数据目录；工作区中的 `/app/data` 指向这里 |
| `COWORKER_REPOSITORY_URL` | 空 | 非严格离线镜像首次启动时改为在线克隆的仓库地址 |
| `COWORKER_REPOSITORY_REF` | bundle 固定提交或远端默认分支 | 首次初始化后检出的分支、tag 或 commit |
| `COWORKER_REPOSITORY_BUNDLE` | 镜像内 bundle | 显式挂载的自定义 bundle 路径 |

Compose 默认把当前本地 Git checkout 挂载到 `/app`，仓库初始化变量不会覆盖它；入口脚本
同时把 `/app/data` 链接到独立的 `coworker-state` 卷。若 checkout 中已有非空
`data/`，入口脚本会拒绝覆盖；先按[升级与迁移](upgrading.md#迁移-checkout-中现有的-data)
将它导入状态卷。设置
`COWORKER_WORKSPACE_SOURCE=coworker-workspace` 后，命名卷首次创建时会从镜像复制 `/app`
并从 bundle 补齐 Git 元数据。更新镜像时，入口脚本只自动快进干净、未分叉且仍位于镜像
默认分支的托管工作区；本地修改、提交、其他分支和分叉历史保持不变。其他仓库相关变量只在
工作区尚未初始化时生效。`offline` 镜像拒绝让启动初始化器从
`COWORKER_REPOSITORY_URL` 访问网络，但它不是网络沙箱，不会禁止用户明确授权的
Agent Git、搜索、浏览器或集成请求。自定义私有仓库应在受控构建环境生成 bundle，
不要把凭据写进 URL 或镜像构建参数。

## 支持的模型

内置 Provider 类型为 `anthropic`、`openai`、`deepseek`、`qwen`、`zhipu`、`minimax`
和 `opencode-go`；`openai_compatible` 是无内置目录的通用 OpenAI 兼容类型，模型需通过
`model_capabilities` 声明。推荐模型目录只包含对应 Provider 静态标记为支持工具调用的模型；
精确列表会随代码更新，以首次初始化向导和 [`src/coworker/brain/`](../../src/coworker/brain/)
中的 Provider 实现为准。首次初始化也可以手动输入目录外模型，并声明该连接上的模型是否
支持工具调用、图片和视频。向导不会发起可能计费的在线能力探测；主模型必须声明支持工具
调用。初始化后可在“运行设置 → 模型与 Provider”继续维护这些能力。

只有在对应 API Key 存在时，该 Provider 才会被注册。`LLM__DEFAULT_PROVIDER`
必须指向已注册的 Provider 实例名。

### 多实例 Provider（providers.json）

上面的扁平字段（`LLM__ZHIPU_API_KEY` 等）每种类型只能配一份。若需要**同一类型的多个实例**（例如多个智谱 Key 面向不同用户），在 `LLM__PROVIDERS_FILE` 指向的 JSON 文件里按 `name` 列举即可。每个 Provider 的「类型（API 方言/模型表）」与「注册名（注册表 key、`default_provider`/`switch_model` 引用的名字）」由此解耦：

```json
[
  { "name": "zhipu-userA", "type": "zhipu", "api_key": "...", "default_model": "glm-5.1", "model_capabilities": [{ "model": "custom-omni-model", "tools": true, "vision": true, "video": false }] },
  { "name": "zhipu-userB", "type": "zhipu", "api_key": "...", "base_url": "...", "default_model": "glm-4.7" }
]
```

字段：`name`（必填，注册名，需唯一）、`type`（必填，取内置 Provider 类型
`anthropic` / `openai` / `deepseek` / `qwen` / `zhipu` / `minimax` /
`opencode-go`，或通用 `openai_compatible`）、`api_key`、`base_url`（可选）、
`default_model`（可选，`switch_model` 切到该实例但不指定模型时使用），以及
`model_capabilities`（可选的模型能力声明列表）。
每条能力声明包含精确 `model` ID 和 `tools`、`vision`、`video` 布尔值；显式声明覆盖
Provider 类型的内置判断，未声明模型继续使用内置目录。视频能力要求同时启用视觉能力。
`openai_compatible` 没有内置目录，因此必须配置 `default_model`，并至少为它声明
`tools` 能力。

- 扁平字段仍然有效，会自动并入为 `name == type` 的默认实例；文件中的同名条目按 `name` 覆盖它。
- 文件不存在则忽略，老配置零改动照常运行。
- 完整示例见仓库根目录 `providers.json.example`。
