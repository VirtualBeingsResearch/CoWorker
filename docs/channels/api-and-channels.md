# API 与通信入口

中文 · [English](api-and-channels.en.md)

[← 返回通信与客户端](README.md)

> 当前 v0.x 版本只应在本机或可信网络使用。部署前请阅读
> [安全策略](../../SECURITY.md)。

所有出站通信先由 `ChannelRegistry` 路由到独立传输信道，例如 Stream、企业微信或微信 Claw。进入 Stream 后，Desktop participant 由 `StreamChannel` 交给内置 Desktop profile 处理。Coworker Desktop 共享 Stream Runtime 的注册、连接、队列与生命周期，并使用现有 participant ID 和消息协议。`list_connections` 聚合各信道及 profile 当前在线或已知可达的通信对象。`/status` 报告运行、模型与用量状态，连接发现通过 `list_connections` 完成。

向内置 Stream、Desktop、企业微信或微信 Claw 信道发送消息时，`communicate` 只接受 `list_connections` 中存在的完整 participant ID（信道明确支持的精确简写仍可使用）。未知 ID 不会被自动纠正，也不会发送消息：如果与已知 ID 的编辑距离不超过 4 个字符，工具会列出相近的完整 ID 供模型重新选择；否则按不存在处理并提示重新调用 `list_connections`。已经注册但当前离线的 Stream participant 仍属于已知对象，可继续使用 outbox 投递。

## Channel 开发模型

`from coworker.channels import BaseChannel, ChannelAccessController, ChannelActivityStore, ChannelCapabilities, ChannelRuntime, ChannelModule, ChannelManagement, ChannelSettings, StreamProfile, create_channel_system` 是稳定的开发入口。`create_channel_system(outbox_dir, activity_path=None, access_config=None)` 是应用唯一的通信装配入口，返回：

- `registry`：注册 Channel、路由 inbound/outbound，并确保共享 Runtime 只启动和停止一次。
- `stream_runtime`：承接 WS/SSE 连接、participant 注册、附件存储和离线 outbox，并向 HTTP 与 WebSocket 路由提供 Stream 基础设施。
- `activity`：记录 participant 最近成功发送与接收时间。传入 `activity_path` 时使用原子 JSON 持久化，应用重启后仍可恢复。
- `access`：共享的 participant 入站/出站访问控制器；Registry、Channel 与 Stream profile 使用同一份配置。
- `modules`：保存完整信道模块贡献的管理接口和热设置应用器。

新增独立传输时继承 `BaseChannel`。只需要传输时可调用
`channel_system.registry.register(channel)`；同时拥有连接管理或热设置时，应实现
`ChannelModule` 并调用 `channel_system.install(module)`，一次注册 transport、可选
`ChannelManagement` 和可选 `ChannelSettings`。Admin 只通过通用
`/api/admin/channels/{channel}/management` 路由快照与命令；配置热应用遍历模块声明的
`config_key`，两者都不解释信道私有语义。Channel 负责 participant 解析、原始入站归一化和出站语义；可变连接状态、后台任务及启停逻辑放在它的 `runtime`。如果只是 Stream 上的新协议行为，则继承 `StreamProfile` 并调用 `channel_system.register_stream_profile(profile)`；profile 负责自己的 participant 前缀、能力、入站归一化和出站修饰，并复用 `StreamRuntime`。Desktop 是内置的 Stream profile。注册边界会一次性报告名称、前缀、基类、Runtime 与重复项等全部配置问题。`CommunicateTool` 将模型工具调用转换为 Registry 出站请求。

需要教给 Agent 的稳定信道操作可以由 Channel 覆写 `agent_instructions()` 提供。Registry 只聚合已启用信道贡献的文本，`SystemPromptBuilder` 将其放入缓存稳定的 `[CHANNELS]` 段；不要把动态连接列表或轮询状态注入系统 Prompt。实时 participant 仍通过 `list_connections` 发现，动作解释和执行仍属于目标 Channel，Registry 不检查 `extra` 的信道私有结构。

最小出站 Channel 只需继承 `BaseChannel` 并实现 `send`；默认已包含空 Runtime、无简写解析、无入站、无连接列表和 activity 辅助方法：

```python
from coworker.channels import BaseChannel, create_channel_system
from coworker.core.types import CommunicateRequest, ToolResult


class TeamChannel(BaseChannel):
    name = "team"
    participant_prefix = "team:"

    async def send(self, request: CommunicateRequest) -> ToolResult:
        await deliver_to_team(request.participant_id, request.message)
        return ToolResult(tool_call_id="", content="sent")


channels = create_channel_system("data/outbox")
channels.registry.register(TeamChannel())
```

只包装现有异步发送函数时，不需要再定义一个 Channel 类：

```python
channels.registry.register(BaseChannel.from_sender("team:", send_to_team))
```

应用内置的 Stream、Desktop 与 WeCom 共享 `channels.activity`。自定义 Channel 如果也要让 `list_connections` 跨重启保留最近收发时间，可在构造时传入 `activity=channels.activity`，并只在入站已接受或出站已成功后调用 `record_received` / `_record_sent`；失败尝试不会污染活动时间。

Channel 通过 `ChannelCapabilities` 声明是否支持 `conversation_id`、`attachments` 和 `extra`，默认仅支持 `message`。Registry 会在发送前统一省略目标不支持的可选字段：只要仍有正文或其他受支持内容，就继续投递，并在工具结果中明确告诉 AI 哪些字段未传递；不会因附件或 `extra` 不受支持而丢掉正文。

## 信道访问列表

`CHANNEL_ACCESS` 按信道配置 participant 的入站和出站白名单/黑名单；也可以在管理端“信道访问”中修改并立即生效：

```env
CHANNEL_ACCESS={"wecom":{"inbound_allow":["wecom:trusted:*"],"inbound_deny":["wecom:trusted:blocked"],"outbound_allow":[],"outbound_deny":["wecom:external:*"]},"desktop":{"inbound_allow":[],"inbound_deny":[],"outbound_allow":["coworker-desktop:*:local:*"],"outbound_deny":[]}}
```

每个信道都有 `inbound_allow`、`inbound_deny`、`outbound_allow`、`outbound_deny` 四个列表。规则按大小写敏感的完整 participant ID 匹配，支持 `*`、`?` 和 `[...]`；没有通配符的值是精确匹配。判定顺序为：命中 deny 时拒绝；否则 allow 非空时必须命中 allow；否则允许。因此未配置某个信道、配置 `{}`，或四个列表都为空时均保持“全部允许”的兼容行为。

内置配置键是 `stream`、`desktop`、`wecom` 和 `weixin`；Stream profile 使用自己的信道名，所以 Desktop participant 受 `desktop` 规则而不是 `stream` 规则约束。扩展 Channel 使用其注册名。入站拒绝发生在附件下载、回复帧/上下文令牌缓存、活动记录和 Agent 处理之前；REST `/messages` 返回 `403`，WebSocket 以 `1008` 关闭，企业微信和微信 Claw 则静默丢弃并记录不含正文的日志。出站拒绝由 Registry 强制执行，被拒绝的 participant 也不会出现在 Agent 的 `list_connections` 中，但仍可在管理端编辑规则。

这些列表只表达“某个信道方向上是否允许某个规范 participant 地址”，不是身份认证、租户隔离或“哪些人可以唤醒 Agent”的权限模型。群聊、机器人实例等聚合 participant 也只按它们自己的 participant ID 判定，不会推导到真实人员身份。

企业微信单聊不提供 `conversation_id`，回复时自动使用该用户最新的新鲜 frame。群聊入站事件会把 frame 的 `req_id`（缺失时使用 `msgid`）作为 `conversation_id` 展示给 AI，回复时传回该值即可精确使用对应 frame；如果指定 frame 已过期或不存在，则改用主动消息发送，不会误用同一群聊的其他 frame。群聊发送时不传 `conversation_id` 也始终视为主动消息，不会自动使用缓存的 frame。

企业微信智能机器人目前不支持通过 API @群成员，因此 WeCom Channel 不提供成员提醒能力。

需要入站时覆写 `receive_raw`，归一化为 `IncomingEvent` 后调用 `publish_inbound`；需要后台连接时注入实现了 `start` / `stop` 的 `ChannelRuntime`。Registry 会拒绝重复名称、重复 participant 前缀和启动后的迟到注册，让配置错误在启动阶段直接暴露。

## REST API

```bash
# 发送消息
curl -X POST http://localhost:8000/messages \
  -H "Content-Type: application/json" \
  -d '{"sender_id": "alice", "content": "你好，你是谁？"}'

# 查看状态
curl http://localhost:8000/status

# 切换模型（provider 为已注册的实例名；省略 model_id 则用该实例配置的 default_model）
curl -X POST http://localhost:8000/switch_model \
  -H "Content-Type: application/json" \
  -d '{"provider": "qwen", "model_id": "qwen-plus"}'

# 在线查看/修改 summary、fallbacks、vision 模型配置（写入 LLM__RUNTIME_CONFIG_FILE）
curl http://localhost:8000/model_config
curl -X PATCH http://localhost:8000/model_config \
  -H "Content-Type: application/json" \
  -d '{"summary":{"provider":"deepseek","model":"deepseek-v4-flash","thinking":false},"fallbacks":["zhipu-userB","deepseek/deepseek-chat"],"vision":{"provider":"anthropic","model":"claude-sonnet-4-6","thinking":false}}'

# 在线回溯记忆树（从原始日志全史重建多尺度记忆树，后台运行）
curl -X POST http://localhost:8000/backfill_tree \
  -H "Content-Type: application/json" \
  -d '{"max_leaves": 64}'

# 查询回溯进度（{running, done, total}）
curl http://localhost:8000/backfill_tree
```

`/status` 响应中的 `usage_stats` 会返回 today / last_7_days / lifetime 三个窗口。每个窗口同时提供
`by_model`（按模型名合并）和 `by_provider_model`（按 `provider/model` 精确区分）；
同时在 `by_scope` 中拆出 `main` / `summary` / `vision` / `bubble` / `subconscious` / `mem0`
六类来源统计，结构与窗口总账一致。窗口总账与 `by_scope` 均包含 `thinking_calls`、
`thinking_seconds`、`avg_thinking_seconds`，用于展示有 `thinking_start -> llm_response`
生命周期的平均思考耗时；summary / vision / mem0 等无起点事件的辅助调用不计入该均值。
升级前的历史日志缺少 provider 时会归入 `unknown/<model>`；升级到来源拆分统计时会优先从日志重建，
若原始日志已丢失则无法恢复旧聚合数据的来源归属。

也可以使用交互式示例：

```bash
uv run python examples/api.py
```

## WebSocket

```javascript
const ws = new WebSocket("ws://localhost:8000/ws/alice");
ws.onmessage = (event) => console.log("收到:", event.data);
ws.send("你好！");
```

同一个 `participant_id` 同一时间只允许一个 SSE/WS 长连接，按先到先得处理。后来的同名 WebSocket 会收到“连接被拒绝”提示并以 `1008` 关闭；后来的同名 SSE 会收到一条拒绝事件后结束。关闭已有连接后即可用相同 ID 重新连接。

### 泡泡直接转交

绑定了同一 `participant_id`（以及可选 `conversation_id`）的活跃 Bubble 会接收匹配的 WebSocket 或 REST 入站消息，并把直接回复投递回该 ID 的在线流。SSE 是单向出站流：客户端订阅 `/sse/{participant_id}` 后，应通过 `POST /messages` 以相同的 `sender_id` 发送后续消息；它们仍会直接转交给 Bubble。

按通信对象启用透明转交时，配置大小写敏感的整串 glob：

```env
AGENT__BUBBLE_HANDOFF_TRANSPARENCY_PARTICIPANT_MATCHES=["wecom:*","weixin:*","coworker-desktop:*:local:*"]
```

`*`、`?` 和 `[...]` 是 glob 通配符；不含通配符的条目表示精确 `participant_id`。上述默认值透明企微、微信 Claw 和 Desktop `local` actor，设为 `[]` 可关闭这些默认匹配。历史版本保存的旧默认列表会随默认值演进；任何自定义列表（包括显式 `[]`）保持原样。

所有在线通用 WebSocket/SSE 会话默认启用透明 Bubble 生命周期：Bubble 首次收到该会话的新消息，或首次准备直接回复时，才会发送接管提示；只有接管提示成功发送后，Bubble 结束时才会发送对应的结束提示。仅创建或绑定 Bubble 不会产生外部通知。对应默认配置为：

```env
AGENT__BUBBLE_HANDOFF_TRANSPARENCY_STREAM_TRANSPORTS=["websocket","sse"]
```

只填写其中一项即可只启用该传输层，设为 `[]` 可全部关闭。Desktop 身份不会回退到这条通用规则：它必须显式命中 participant glob，因此默认只透明 `coworker-desktop:<desktop_id>:local:…`，不会透明 `claude` 或 `codex` actor。

支持结构化 `extra` 的出站通道（通用 WebSocket/SSE 与 Desktop）还会在透明转交消息的 `extra.bubble` 中携带来源，前端应优先使用它渲染接管状态，而不是解析提示文案：

```json
{
  "message": "🫧 当前会话已转交给泡泡处理……",
  "extra": {
    "bubble": {
      "id": "bbl_260719120000",
      "kind": "handoff",
      "phase": "start",
      "resumed": false
    }
  }
}
```

已公告的接管在结束时使用 `phase: "end"`；Bubble 直接回复使用 `kind: "reply"`。不支持结构化 `extra` 的普通信道（如企业微信和微信 Claw）不会收到这段元数据，仍通过接管/结束文本与 `🫧 泡泡：` 回复前缀标识来源；Desktop 已保证消费结构化元数据，因此接收原始正文，不注入也不解析该前缀。

`coworker-desktop:*` participant 的消息、注册、SSE 和 WebSocket 在默认生产模式下都要求
`Authorization: Bearer <API__COMMUNICATION_TOKEN>`。未单独配置通信令牌时，服务端会回退使用
管理员令牌，方便本机首次连接；需要隔离权限时应显式配置独立令牌。只有将服务端和 Desktop 配置都显式设为
`development_mode=true` 才会关闭这层校验；该模式仅适用于回环地址的本机调试。

浏览器示例：

- `examples/chat.html`
- `examples/api_test.html`

## 文件消息

将消息文件放入 `data/inbox/`，Agent 会在轮询时读取并处理。回复会写入 `data/outbox/`，WebSocket 在线用户也会收到推送。
