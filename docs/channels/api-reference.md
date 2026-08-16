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
| `POST /messages` | 配置通信令牌后要求 `Authorization: Bearer <API__COMMUNICATION_TOKEN>`；未配置时依赖回环/可信网络边界 |
| `GET /status` | 未携带有效 Bearer 时返回基础生命周期信息；携带后返回完整模型与用量快照 |
| Desktop participant、Desktop 注册、Relay 内层请求 | `Authorization: Bearer <API__COMMUNICATION_TOKEN>` |
| `/api/admin/*` 与配置导出 | 管理员令牌 |
| Desktop 发布管理 | Desktop update 管理令牌或管理员令牌 |

未单独配置通信令牌时，首次运行流程会回退使用管理员令牌。长期使用应设置独立的
`API__COMMUNICATION_TOKEN`。`API__DEVELOPMENT_MODE` 不会关闭 API 通信校验。

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
# 无 Bearer 时只返回基础状态；携带有效令牌返回完整快照
curl http://127.0.0.1:8000/status \
  -H "Authorization: Bearer <API__COMMUNICATION_TOKEN>"

curl -X POST http://127.0.0.1:8000/switch_model \
  -H "Content-Type: application/json" \
  -d '{"provider":"deepseek","model_id":"deepseek-chat"}'
```

未认证的 `/status` 返回 `status`、`is_running`、`is_sleeping`、`setup_mode`、
`communication_token_configured` 和 `authenticated`；不包含 Provider、模型配置与用量。
携带有效 Bearer 时返回完整快照。字段会随可用模块增加，客户端应容忍未知字段。对需要
稳定审计的集成，保存原始响应和 Coworker 版本。

## SSE 与 WebSocket

- WebSocket：`ws://127.0.0.1:8000/ws/{participant_id}`，双向文本消息。
- SSE：`GET /sse/{participant_id}`，只负责出站；入站仍用相同 ID 调用 `POST /messages`。
- 同一 `participant_id` 同时只允许一个 SSE 或 WebSocket 长连接，后来的连接会被拒绝。
- SSE 每 15 秒发送注释心跳；代理应关闭响应缓冲。
- `coworker-desktop:*` ID 必须携带通信 Bearer。浏览器原生 `EventSource` 无法设置
  Authorization Header，因此不要用它承载受保护的 Desktop participant。

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

`/api/admin/*`、`/api/desktop-updates/*` 和 `/api/export_config` 会修改配置、恢复状态、
发布制品或导出包含密钥的数据。除非你在开发同版本官方管理端，否则优先使用 Web 界面；
调用前阅读 [Web 管理后台](../guides/README.md) 和[数据与信任边界](../architecture/data-boundaries.md)。

[← 返回项目首页](../../README.md)
