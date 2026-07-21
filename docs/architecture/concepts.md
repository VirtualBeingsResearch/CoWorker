# 核心概念与能力

中文 · [English](concepts.en.md)

[← 返回架构与核心概念](README.md)

## 当前能力

- **多 LLM Provider**：支持 Anthropic、OpenAI、DeepSeek、Qwen、Zhipu、MiniMax，可通过 API 或工具热切换模型；可经 `providers.json` 配置同一类型的多个命名实例（如多个智谱 Key），每个实例可带各自的默认模型。
- **分层记忆**：短期上下文自动压缩，长期记忆由 **mem0** 管理（底层 ChromaDB + 本地 SentenceTransformer），具备自动去重与语义合并；收到新消息时系统自动检索相关记忆以 `[自动回忆]` 形式注入上下文，已回忆/已写入的记忆在同一会话内不重复注入（持久化去重，重启后同样有效）；短期记忆在重启后自动恢复。
- **多人对话隔离**：每个 `participant_id` 拥有独立对话线程，避免不同用户的上下文互相污染。
- **三类交互入口**：文件 inbox/outbox、REST API、SSE/WebSocket 实时通信。
- **工具系统**：文件读写、代码执行、网页搜索、浏览器自动化、记忆读写、技能读取、任务板、模型切换等。
- **视觉分析**：配置 `LLM__VISION_PROVIDER/MODEL` 后，纯文本模型（如 DeepSeek）可调用 `visual_analyze`，委托视觉模型理解图片或视频；视频以 Base64 原生输入发送，仅支持声明了视频能力的视觉模型，编码后达到 10 MiB 时会先尝试用 FFmpeg 压缩。
- **泡泡思考**（可选）：设置 `AGENT__BUBBLE_THINKING=true` 后，模型可主动从当前上下文分叉出独立子任务并发执行，完成后自动合并结论；支持主线与泡泡之间双向通信。创建时绑定 `participant_id`（可同时绑定 `conversation_id`）后，匹配且无歧义的后续通信会直接交给该活跃泡泡处理；泡泡只能直接回复其绑定对象。可用 `AGENT__BUBBLE_HANDOFF_TRANSPARENCY_PARTICIPANT_MATCHES` 通过整串 glob 为指定通信 ID 启用外显提示；在线 WebSocket/SSE 会话默认也会按传输层启用，可通过 `AGENT__BUBBLE_HANDOFF_TRANSPARENCY_STREAM_TRANSPORTS` 调整或关闭。默认 participant glob 只匹配企微与 Desktop `local` actor，不匹配 Claude 或 Codex actor。转交、直接回复和结束都会标明来自泡泡。达到轮次上限后，可在配置的宽限期内通过 `bubble_spawn(bubble_id=...)` 保留原上下文继续执行。
- **潜意识思考**（可选）：设置 `AGENT__SUBCONSCIOUS_THINKING=true` 后，系统自动在后台触发多类反省——**自我审计**、**经验总结**（仅在短期记忆压缩前提炼经验写入长期记忆）、自由发散、技能库审视、宫殿园丁等。各模式的触发节奏和行为写在 `.coworker/subconscious/*/MODE.md`，环境变量只保留总开关、压缩前总结开关和通用 `max_cycles` 兜底。整个过程静默运行，不打扰主线工作流。
- **技能系统**：从 `.coworker/skills` 加载 `SKILL.md` 风格的自然语言操作指南。
- **记忆宫殿（Memory Block Tree）**：从 `.coworker/palaces` 加载 `PALACE.md` 领域包。每个宫殿是一个领域的「组合层」——一张薄薄的领域速记卡，加上指向 skill（程序）和长期记忆（事实）的指针。系统提示中只常驻薄注册表（名字 + 何时挂载），完整宫殿在执行任务的「泡泡」里按需注入：关键 skill 强加载、相关 skill 列名待按需加载、按 `memory_tags` 召回相关长期记忆。泡泡成功收尾时，其结论会按宫殿标签自动写回长期记忆，使宫殿随任务执行持续「生长」。
- **身份系统**：从 `data/identity` 加载名字和人格；首次启动时可以处于未命名的新生状态。

## 主要工具

启动时默认注册：

- 文件工具：`read_file`、`write_file`、`list_directory`、`find_files`、`grep_files`
- Web 工具：`search_web`、`fetch_url`
- 浏览器工具：`browser_open`、`browser_screenshot`、`browser_action`、`browser_get_content`、`browser_close`、`browser_list_sessions`
- 代码工具：`execute_code`、`get_code_result`、`kill_code_job`（`execute_code` 默认最多等 2 秒；`block=true` 仅泡泡上下文生效，主线传入会被忽略。`get_code_result` 只返回当前状态，不负责等待；需要等待时应先调用 `sleep` 再重试）
- 记忆工具：`query_memory`（综合搜索：query 同时检索最近活动索引和长期记忆；start/end 回忆或过滤时间窗；query 可与 start/end 同用）、`manage_memory`、`clear_short_term_memory`（手动全量压缩 primary，不删除记忆）、`manage_pinned_context`
- 系统工具：`sleep`、`switch_model`、`get_context`、`restart_self`
- 闹钟工具：`set_alarm`、`list_alarms`、`cancel_alarm`
- 通信工具：`communicate`、`list_ws_connections`
- 技能与任务：`get_skill`、`task_board`

`visual_analyze` 默认注册，文本和视觉主模型均可见；配置 `LLM__VISION_PROVIDER` + `LLM__VISION_MODEL` 后可用：

- `visual_analyze`：对图片或视频进行视觉分析与推理，支持本地路径和 HTTP(S) URL；视频要求视觉模型支持原生视频输入，Base64 Data URL 必须小于 10 MiB，超限时尝试用 FFmpeg 压缩

仅对有视觉能力的模型可见：

- `view_image`：主动加载本地路径或 URL 图片直接查看，支持 `full_resolution` 参数
- `browser_view`：对浏览器当前页面截图并直接查看，支持 `full_resolution` 参数

## 目录结构

```text
coworker/
├── .coworker/skills/          # 技能知识库
├── .coworker/palaces/         # 记忆宫殿（领域速记卡）
├── examples/                # API 与 WebSocket 示例
├── scripts/                 # 辅助脚本
├── src/coworker/
│   ├── agent/               # 主循环、inbox 监听、交互日志
│   ├── api/                 # FastAPI REST API + WebSocket
│   ├── brain/               # 多 Provider LLM 抽象
│   ├── core/                # 配置、类型、异常
│   ├── identity/            # 身份加载
│   ├── memory/              # 短期记忆、长期记忆、向量嵌入
│   ├── palaces/             # 记忆宫殿扫描与读取
│   ├── prompts/             # System Prompt 构建
│   ├── skills/              # 技能文件扫描与读取
│   └── tools/               # 工具实现与注册
├── tests/                   # 单元测试
└── data/                    # 运行时数据，启动后自动创建或写入
    ├── inbox/               # 文件消息输入
    ├── outbox/              # 文件消息输出
    ├── memory/              # mem0/ChromaDB 持久化 + short_term_snapshot.json
    ├── identity/            # name.txt、personality.md 等
    ├── logs/                # 日志和 interactions*.jsonl 分片
    └── task_board.md        # 任务板工具使用的文件
```

## 记忆系统

短期记忆分为 Agent 自身思考流和按参与者隔离的对话线程。当 token 使用量达到 `MEMORY__COMPRESS_THRESHOLD` 时，系统压缩较早消息——压缩走下文的**多分辨率记忆树**，同时由潜意识在压缩前提取值得长期保留的内容写入 mem0。

### 多分辨率记忆树（短期记忆 LOD）

> 注意：这与下文的「记忆宫殿（Memory Block Tree）」是两个不同的东西，只是名字都带 block tree——这里是**短期记忆内部**的时间尺度脊柱，宫殿是领域**组合层**。

借鉴游戏 LOD/mipmap（近处全细节、越远越粗）的思路映射到时间轴：短期记忆压缩不再把最老一段塌缩成**单条**摘要，而是把它提升为一片**树叶**，按时间尺度做**二进制级联合并**（两个同级节点合并成更粗的上一层），形成一座多分辨率脊柱——近期是原始消息（全细节），越久的历史用越来越粗的摘要表示。喂给模型的上下文 = `每个时间尺度各一条带时间标签的摘要 + 原始尾部`，规模 O(log N)，让 Agent 对「上周在做什么 / 这一整天的主线」这类**更大时间尺度**保持把握。合并时优先**从原始日志按时间区间重新摘要**（而非摘要叠摘要），避免逐层退化。

- **时间窗回忆 / 综合搜索**：`query_memory(start=..., end=...)` 优先使用短期记忆树摘要回忆某段历史；没有摘要时回退原始日志并生成摘要。`query_memory(query=...)` 会同时检索最近活动索引和 mem0 长期记忆，默认合计返回 5 条；`query_memory(query=..., start=..., end=...)` 会在指定时间窗内做语义聚焦搜索。查询内联结果硬限制为 3000 字符，完整冻结结果会写入临时 Markdown，并提供 `read_file` 路径和章节行号供稳定分页、展开。无参数调用会报错，需提供 `query` 或同时提供 `start/end`。
- **手动全量压缩**：`clear_short_term_memory` 会把当前 `primary` 中尚未压缩的实时消息整体压进记忆树，释放上下文空间但不删除记忆；压缩前同样会触发潜意识 `summarize` 提炼长期记忆；正在执行工具时会保留末尾 `tool_use` 以维持消息结构。
- **历史回溯**（升级迁移）：升级后默认不回溯，记忆树从新压缩开始往后长。要把**已有历史**也建成多尺度树，读取全部 `interactions*.jsonl` 分片、按时间分块逐块摘要成叶子、级联重建脊柱（生成叶子数受 `MEMORY__TREE_BACKFILL_MAX_LEAVES` 封顶）。两种方式：
  - **离线**（进程未运行时）：`uv run python -m coworker --backfill-tree`，重建后写回快照退出。
  - **在线**（运行中）：`POST /backfill_tree`（请求体可带 `{"max_leaves": 64}`）。运维触发、对模型零 token 成本；后台异步重建、不阻塞，逐块打进度日志，可用 `GET /backfill_tree` 轮询进度（`{running, done, total}`），完成后记日志并向 inbox 推送系统消息（重复触发返回 409）。安全性由「临时树构建 + 压缩锁内原子替换」保证：全程不碰活树，替换时保留构建期间新压缩的节点。⚠️ 不要在进程运行时跑离线 CLI——两者会争用快照文件。

- **回退**：设 `MEMORY__TREE_ENABLED=false` 即回退到旧的「单条压缩摘要锚点」行为。

**Pin 消息**：通过 `manage_pinned_context` 工具，模型可以将重要文本或文件内容"pin"住。Pin 的消息以真实消息形式存在于对话流中；当短期记忆压缩将其压掉后，下一个 cycle 会自动检测到缺失，把它重新 append 到 primary 末尾作为最新输入，起到保留缓存命中和定期强调的作用。文件 pin 在每次重注入时重新读取文件，始终保持最新内容。Pin 状态随快照持久化，重启后自动恢复。

长期记忆由 **mem0** 管理，底层持久化到 `MEMORY__DB_PATH`（ChromaDB），嵌入默认使用本地 `sentence-transformers/paraphrase-multilingual-mpnet-base-v2`（首次使用自动加载）。mem0 在写入时自动对语义相近的记忆进行合并/去重，避免重复积累。

**最近活动索引**：系统会把最近 `MEMORY__RECENT_ACTIVITY_DAYS` 天内、已经从短期 `primary` 压缩出去的交互日志写入独立的 `recent_activity_v1` Chroma collection，用于回忆“最近做过什么、工具结果是什么、哪里报错、最后状态如何”。记忆压缩完成后会调度后台索引任务；仍以原文形式保留在短期记忆里的活动不会重复入索引。普通事件按单条日志事件入索引；工具调用会和对应工具结果合并为同一条证据，保证参数、状态和结果一起被召回。召回时，语义命中只作为锚点，系统会从原始日志补齐同一段“收到消息 → 可见回复 → 工具调用与结果”的历史活动链，再以活动回放形式注入；内部推理、检索分数和切片信息不会进入回放。`message_tick`、系统提示、自动回忆、模型 usage 等噪声不会入索引。超长工具证据对外仍是一条证据，但内部按当前 embedder 的 tokenizer token 滑窗切片（默认 120 tokens、重叠 24 tokens），避免超过 embedding 模型输入长度后尾部细节搜不到。

**自动回忆**：每次收到用户消息，系统会用消息文本语义检索长期记忆，将相关度高于阈值的结果以 `[自动回忆]` 消息注入上下文；同时可用更严格阈值检索最近活动，并以 `[自动回忆·历史活动回放]` 注入少量高相关片段。已回忆或已通过 `write_memory` 写入的记忆 ID 存储在 `Message.recalled_memory_ids` 中，随快照持久化，同一会话（包括重启后）不会重复注入。

**数据迁移**：首次升级到 mem0 版本前，旧 ChromaDB 数据格式与 mem0 不兼容，需清空旧数据：
```bash
rm -rf data/memory/
# 或在 .env 中修改 MEMORY__DB_PATH=data/memory_v2
```

### 重启状态继承

短期记忆每个 cycle 结束后自动快照到 `data/memory/short_term_snapshot.json`，关闭时再做一次兜底保存。下次启动时若快照存在，会自动恢复对话历史，并清理末尾未完成的工具调用残缺链。同时向 Agent 注入一条系统重启通知，告知当前时间及恢复的闹钟数量。

Agent 可通过 `restart_self` 工具主动触发安全重启（例如在更新自身代码后）。重启流程：先用 `--check` 模式校验代码环境，通过后保存快照并原地重启进程，新进程启动后短期记忆完整恢复，`restart_self` 的工具调用结果会被替换为真实的重启成功消息（含恢复消息数和闹钟数）。

闹钟状态持久化到 `data/memory/alarms.json`，重启后自动恢复调度。错过触发时间的闹钟会立即补发，消息中标注迟到时长；循环闹钟仅补发一次，之后按原间隔继续运行。

如需全新启动，删除快照文件即可：

```bash
rm data/memory/short_term_snapshot.json
rm data/memory/alarms.json  # 同时清除闹钟状态（可选）
```

## 记忆宫殿（Memory Block Tree）

记忆宫殿用来把「专业任务执行」的领域上下文从「长期运行」的对话流里隔离出去，避免两者互相污染。一个宫殿不是新的存储，而是一个**组合层**：把领域速记卡、关键 skill、相关长期记忆组装成一个可挂载的单元。

宫殿放在 `.coworker/palaces/<name>/PALACE.md`，结构与 skill 类似：

```markdown
---
name: product-bug
when_to_attach: 用户反馈示例产品的缺陷/异常，需要核实并走问题提交流程创建 bug 单
critical_skills: [bug-create]             # 派生泡泡时强加载完整 body
related_skills: [issue-tracker, product-config]  # 卡片里列名，泡泡按需 get_skill
memory_tags: [product, bug]                # 按标签召回相关长期记忆
---

# 领域速记卡（薄，≤ ~800 token）
# 只放 orientation：领域心智模型、易错点、指针、何时反问。
# 操作步骤进 skill，具体事实进 mem0——卡片只让模型知道在干什么、该去哪拿其余的。
```

**工作方式**：

- 系统提示中只常驻 `[PALACES]` 注册表（名字 + `when_to_attach`），保持前缀缓存稳定。
- 主线遇到匹配某宫殿的专项任务时，用 `bubble_spawn(palaces=[...])` 派生泡泡执行（专项执行建议 `fresh_start=true`，得到干净的领域上下文），可同时挂多个宫殿。
- 泡泡启动时注入：关键 skill 的完整 body、宫殿速记卡、按 `memory_tags` 过滤召回的长期记忆。
- 泡泡成功收尾时，其结论按宫殿 `memory_tags` 自动写回 mem0（确定性钩子），下次挂同一宫殿时由标签召回自动捞回——宫殿因此随任务执行持续「生长」。
- 潜意识里有个**宫殿园丁**（subconscious `garden` 模式）：巡检某个宫殿的领域记忆，剪除过期/矛盾/冗余、整合补写。更新主频由 `garden/MODE.md` 里的 `use_threshold`、`every_seconds`、`min_interval_seconds` 控制。园丁串行执行、卡片只读（要改卡片只向主线提建议）。

宫殿目录可通过 `AGENT__PALACES_DIR` 配置（默认 `.coworker/palaces`）。

> 注：`.coworker/palaces/` 当前不纳入版本管理（卡片由模型自维护，形态尚在观察期）。仓库不附带示例宫殿，本节示例仅作格式说明。
