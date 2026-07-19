# API 与通信入口

中文 · [English](api-and-channels.en.md)

[← 返回项目首页](../README.md)

> 当前 v0.x 版本只应在本机或可信网络使用。部署前请阅读
> [安全策略](../SECURITY.zh-CN.md)。

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

`/status` 响应中的 `usage_stats` 会返回 today / last_7_days / lifetime 三个窗口。每个窗口保留旧版
`by_model`（按模型名合并），并新增 `by_provider_model`（按 `provider/model` 精确区分）；
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

`coworker-desktop:*` participant 的消息、注册、SSE 和 WebSocket 在默认生产模式下都要求
`Authorization: Bearer <API__COMMUNICATION_TOKEN>`。只有将服务端和 Desktop 配置都显式设为
`development_mode=true` 才会关闭这层校验；该模式仅适用于回环地址的本机调试。

浏览器示例：

- `examples/chat.html`
- `examples/api_test.html`

## 文件消息

将消息文件放入 `data/inbox/`，Agent 会在轮询时读取并处理。回复会写入 `data/outbox/`，WebSocket 在线用户也会收到推送。
