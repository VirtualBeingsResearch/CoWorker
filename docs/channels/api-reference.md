# API 参考

中文 · [English](api-reference.en.md)

[← 返回通信与客户端](README.md)

本页记录供本地集成使用的 HTTP、SSE 和 WebSocket 契约。Channel 扩展模型与完整行为说明见
[API 与通信入口](api-and-channels.md)。当前 v0.x 没有企业级多租户授权边界，不要把 API
直接暴露到公网。

## OpenAPI

初始化完成后，FastAPI 默认提供：

- OpenAPI JSON：`GET /openapi.json`
- Swagger UI：`GET /docs`
- ReDoc：`GET /redoc`

首次设置尚未完成时，普通路由会重定向到 `/admin`。自动 schema 包含管理端实现接口；
`/api/admin/*` 主要供同版本 Web 管理后台使用，不承诺独立客户端的长期兼容。

## 认证范围

| 接口 | 默认认证 |
|---|---|
| `POST /messages` | 显式设置 `API__COMMUNICATION_TOKEN` 后要求 Bearer；未显式设置时依赖回环/可信网络边界 |
| `GET /status` | 显式设置令牌且未携带有效 Bearer 时返回基础生命周期信息；未显式设置或携带有效 Bearer 时返回完整快照 |
| `GET /profile` | 显式设置令牌后要求 Bearer；未显式设置时不校验 |
| `GET /logs/stream` | 显式设置令牌后要求 Bearer；未显式设置时不校验 |
| `WebSocket /ws/{participant_id}` | 显式设置令牌后所有连接要求 Bearer；未显式设置时仅 `coworker-desktop:*` 要求 |
| `SSE /sse/{participant_id}` | 显式设置令牌后所有连接要求 Bearer；未显式设置时仅 `coworker-desktop:*` 与 Relay 内层请求要求 |
| Desktop participant、Desktop 注册、Relay 内层请求 | `Authorization: Bearer <API__COMMUNICATION_TOKEN>`（未显式设置时回退管理员令牌） |
| `/api/admin/*` | 管理员令牌 |
| Desktop 发布管理 | Desktop update 管理令牌或管理员令牌 |

只有显式设置 `API__COMMUNICATION_TOKEN` 后，普通 REST 消息、完整状态快照、身份档案、运行日志流、
WebSocket 和 SSE 连接才启用通信 Bearer 校验；未显式设置时这些接口保持旧行为。Desktop 通信未显式设置
令牌时回退使用管理员令牌。长期使用应显式设置独立的 `API__COMMUNICATION_TOKEN`。

## 核心 HTTP 接口

| 方法与路径 | 用途 |
|---|---|
| `POST /messages` | 把消息和可选附件加入 Agent 入站队列 |
| `GET /status` | 返回运行、模型和用量快照 |
| `GET /profile` | 返回身份、自述和最早日志时间 |
| `POST /switch_model` | 切换主线 Provider/模型 |
| `GET/PATCH /model_config` | 读取或修改 summary、fallback、vision 配置 |
| `GET/POST /backfill_tree` | 查询或启动历史记忆树回溯 |
| `GET /backups` | 列出应急短期上下文备份 |
| `POST /backups/restore` | 以 `full` 或 `summarize` 模式恢复应急备份 |
| `GET /api/debug/tasks` | 排查事件循环任务；仅用于受信任的诊断环境 |
| `GET /v1/models` | OpenAI 兼容模型目录，返回 `coworker` |
| `POST /v1/chat/completions` | OpenAI 兼容入站；Bearer 短名映射为 `openai:{短名}` |

`/v1/*` 使用任一通信令牌（主令牌或 extras）鉴权，且始终要求 Bearer。首次设置未完成时返回 JSON `503`，不会 303 到 `/admin`。不进入 Relay 允许列表。可选 `conversation_id` 或 `X-Coworker-Conversation-Id`；省略时由第一条 system 与第一条 user 做窗口指纹。`stream=true` 时把整段 completion 作为 SSE 发出，不是 token 流。详见 [OpenAI 兼容信道](api-and-channels.md#openai-兼容信道)。

### 发送消息

配置了通信令牌（`API__COMMUNICATION_TOKEN`，未单独配置时回退管理员令牌）时，所有
`POST /messages` 请求都必须携带：

```text
Authorization: Bearer <API__COMMUNICATION_TOKEN>
```

```json
{
  "sender_id": "integration:alice",
  "content": "请汇总今天的任务",
  "conversation_id": "daily",
  "attachments": [
    {
      "filename": "notes.txt",
      "media_type": "text/plain",
      "data": "base64-encoded-bytes"
    }
  ]
}
```

普通消息成功后返回：

```json
{
  "status": "queued",
  "sender_id": "integration:alice",
  "conversation_id": "daily"
}
```

`sender_id` 是持续对话隔离边界的一部分，应稳定、可审计且不要复用他人的 ID。附件
`data` 使用 Base64。HTTP 成功只表示已入队，不表示模型已经完成回复。如果 `sender_id`
被对应信道的入站访问列表拒绝，服务端会在解码附件和入队前返回 `403`。

### 状态与模型

```bash
# 已配置令牌时：无 Bearer 只返回基础状态；携带有效令牌返回完整快照
curl http://127.0.0.1:8000/status \
  -H "Authorization: Bearer <API__COMMUNICATION_TOKEN>"

curl -X POST http://127.0.0.1:8000/switch_model \
  -H "Content-Type: application/json" \
  -d '{"provider":"deepseek","model_id":"deepseek-chat"}'
```

管理员配置了通信令牌时，未认证的 `/status` 返回 `status`、`is_running`、`is_sleeping`、
`setup_mode`、`communication_token_configured` 和 `authenticated`；不包含 Provider、模型
配置与用量。未配置通信令牌或携带有效 Bearer 时返回完整快照。字段会随可用模块增加，客户端
应容忍未知字段。对需要稳定审计的集成，保存原始响应和 Coworker 版本。

## SSE 与 WebSocket

- WebSocket：`ws://127.0.0.1:8000/ws/{participant_id}`，双向文本消息。
- SSE：`GET /sse/{participant_id}`，只负责出站；入站仍用相同 ID 调用 `POST /messages`。
- 同一 `participant_id` 同时只允许一个 SSE 或 WebSocket 长连接，后来的连接会被拒绝。
- SSE 每 15 秒发送注释心跳；代理应关闭响应缓冲。
- 显式设置 `API__COMMUNICATION_TOKEN` 后，`/ws/{participant_id}` 和 `/sse/{participant_id}`
  的所有连接都要求 Bearer；未显式设置时仅 `coworker-desktop:*` 与 Relay 内层请求要求。
- `coworker-desktop:*` ID 必须携带通信 Bearer。网页聊天通过带 Authorization 的 fetch 流
  消费通用 SSE，不再依赖无法设置 Header 的原生 `EventSource`。
- 运行日志流 `GET /logs/stream` 在显式设置通信令牌后也要求 Bearer；身份页使用带
  Authorization 的 fetch 流消费该接口。

出站事件包含正文，并可能包含结构化 `extra`，例如 Bubble 接管状态。客户端应优先读取
`extra.bubble`，不要解析本地化提示文字。

## 错误与重试

FastAPI 错误通常为：

```json
{"detail":"错误说明"}
```

| 状态 | 处理 |
|---|---|
| `400/422` | 修正请求、模型或协议字段，不要原样重试 |
| `401/403` | 检查令牌、认证范围和信道访问列表，不要记录完整 Authorization |
| `404` | 检查资源、版本或功能是否启用 |
| `409` | 当前已有任务或连接；先查询状态 |
| `503` | Agent、Channel 或令牌尚未就绪；退避后重试 |

普通 `/messages` 没有通用幂等键。只有 Desktop 协议使用 `message_id` 做有界去重。自定义
集成重试前应避免重复产生有副作用的用户消息。

## 管理与发布接口

`/api/admin/*` 和 `/api/desktop-updates/*` 会修改配置、恢复状态或发布制品。除非你在开发
同版本官方管理端，否则优先使用 Web 界面；
调用前阅读 [Web 管理后台](../guides/README.md) 和[数据与信任边界](../architecture/data-boundaries.md)。

[← 返回项目首页](../../README.md)
