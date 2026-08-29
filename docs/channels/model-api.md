# 模型接口（OpenAI 兼容）

中文 · [English](model-api.en.md)

[← 返回通信与客户端](README.md)

模型接口把 Coworker 暴露成一个 **OpenAI Chat Completions 兼容的"模型"**：任何能接入聊天模型的地方（聊天客户端、IDE 插件、自动化脚本）都可以用标准的 `base_url + api_key` 接入搭档。接入方以为自己在调用一个模型，实际对话的是有记忆、有工具、会主动汇报进度的搭档。

## 启用

模型接口默认关闭。在 `.env` 中配置至少一个令牌后启用：

```bash
MODEL_API__ENABLED=true
MODEL_API__TOKENS='[{"token":"sk-my-long-token","display_name":"Alice"}]'
```

每个令牌对应一个参与者（participant）：

- 配置了 `display_name` 时，participant 为 `api:<名称小写连字符>`（如 `api:alice`）；
- 否则使用令牌哈希前缀 `api:<8 位十六进制>`。

不同令牌 = 不同参与者，直接挂到现有 Persona（人物档案）体系上：首次请求会自动创建 `Person` 并绑定别名，Agent 从第一条消息就能看到人物卡片。所有 `/v1` 请求都必须携带 `Authorization: Bearer <token>`，令牌不匹配返回 401；功能未启用或 Agent 未就绪返回 503。

## 协议语义

接入方沿用 OpenAI 协议，Coworker 补充了少量约定：

### 一轮请求 = 一轮会话

一个 `/v1/chat/completions` 请求开启一轮会话（turn）。Agent 在工作过程中会**连续发送多条回复**（状态、发现、阶段结果），每条都会立即出现在响应流中——搭档像真人一样边干边汇报，而不是沉默很久后一次返回。

### 结束轮次：`end_turn`

默认情况下请求会一直等到 Agent 认为本轮结束。Agent 用最后一条 `communicate` 携带 `extra={"end_turn": true}` 结束本轮，响应以 `finish_reason: "stop"` 收口，并在最后一个 chunk（流式）或响应对象（非流式）中带自定义字段 `coworker_end_reason: "end_turn"`。

### 调用方工具：`tool_calls`

请求中的 `tools` schema 和 system prompt 一样，作为**场景上下文**交给模型自己理解（不会注册为 Agent 内部工具）。当模型需要调用对方应用暴露的工具时，它以 `extra={"tool_calls": [...]}`（OpenAI 格式）回复；响应以 `finish_reason: "tool_calls"` 结束，`coworker_end_reason: "tool_calls"`。调用方应用执行工具后，把 `role: "tool"` 的结果放在下一轮请求的消息里发回来即可，会话自动延续。

### 调用方 system prompt：场景，不是指令

调用方自带的 system prompt 会以「[调用方场景]」块的形式注入 Agent 上下文，**只作为背景信息**，绝不覆盖 Agent 自己的身份与安全边界。场景按内容做哈希去重：同一会话中场景未变化时不会重复注入。

## 会话粘性

OpenAI 协议没有会话 id，Coworker 通过**历史指纹匹配**自动判定会话归属：兼容客户端每轮都会重发完整历史，服务端对每条消息计算指纹，用"请求头部 ≖ 已知历史尾部"的最大重叠来匹配（支持客户端裁剪旧消息的滑窗行为），并生成服务端会话 id。无法匹配任何已知会话时视为新会话。入站事件会携带 `[conversation:...]` 标注，Agent 回复时保持该会话 id 即可路由回正确的请求。

同一会话并发的第二个请求不会被排队拒绝：新消息会作为后续输入**直接投递**给正在进行的会话，新请求挂到同一输出流上，`end_turn` 时一起收口。是否串行发送由客户端自己决定。

## 生命周期

计时按"无输出时长"计算，Agent 持续汇报状态就不会触发：

- **5 分钟无输出**：向 Agent 注入一条系统提醒，提示尽快汇报进度或结束本轮；
- **20 分钟仍无输出**：通知 Agent 该轮的 HTTP 响应已关闭，同时断开流（最后一个 chunk 带 `coworker_end_reason: "timeout"`）。会话本身保留，客户端下次带着历史来仍可续上。

阈值可用 `MODEL_API__NUDGE_SECONDS`（默认 300）与 `MODEL_API__TIMEOUT_SECONDS`（默认 1200）调整。

Agent 未就绪、功能未启用返回 503；所有上游模型候选都失败时，Agent 循环内部的 fallback 耗尽后同样以 503 / 流内错误呈现。非流式请求返回整轮全部回复的拼接。

## 能力与限制

- `GET /v1/models` 返回单个模型 `coworker`，请求中的 `model` 字段接受任意值并原样回显。
- `usage` 为本地估算值，非上游精确计量。
- 多模态消息只提取文本部分；`n>1`、`logprobs` 等参数被忽略。
- 场景（system prompt + tools）注入有长度预算（`MODEL_API__SCENARIO_MAX_CHARS`，默认 6000），超出部分截断并注明。
- 模型接口当前不纳入 Relay 公网隧道白名单；如需公网访问，请使用反向代理并在代理层终止 TLS。
- 未来方向：按会话的并发执行单元（泛化 bubble）、共享资源冲突由 Agent 自行协调的在场感知注入。
