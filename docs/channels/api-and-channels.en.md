# API and Communication Channels

[中文](api-and-channels.md) · English

[← Back to Channels and Clients](README.en.md)

> The current v0.x releases should be used only locally or on a trusted network. Read the
> [security policy](../../SECURITY.md) before deployment.

WeCom messaging is currently provided by the WeCom WebSocket adapter. For its queueing, ordering, and concurrency boundaries—and the proposed evolution toward per-conversation ordering, idempotency, durable inbox/outbox processing, and exact reply correlation—see [WeCom message ordering, reliability, and concurrency design](wecom-message-ordering-and-concurrency.en.md).

## REST API

```bash
# Send a message
curl -X POST http://localhost:8000/messages \
  -H "Content-Type: application/json" \
  -d '{"sender_id": "alice", "content": "Hi, who are you?"}'

# Check status
curl http://localhost:8000/status

# Switch models (provider is a registered instance name; omit model_id to use its default_model)
curl -X POST http://localhost:8000/switch_model \
  -H "Content-Type: application/json" \
  -d '{"provider": "qwen", "model_id": "qwen-plus"}'

# View or change summary, fallbacks, and vision model settings online
# Changes are written to LLM__RUNTIME_CONFIG_FILE
curl http://localhost:8000/model_config
curl -X PATCH http://localhost:8000/model_config \
  -H "Content-Type: application/json" \
  -d '{"summary":{"provider":"deepseek","model":"deepseek-v4-flash","thinking":false},"fallbacks":["zhipu-userB","deepseek/deepseek-chat"],"vision":{"provider":"anthropic","model":"claude-sonnet-4-6","thinking":false}}'

# Rebuild the multiscale memory tree online from the complete raw log history (runs in background)
curl -X POST http://localhost:8000/backfill_tree \
  -H "Content-Type: application/json" \
  -d '{"max_leaves": 64}'

# Query backfill progress ({running, done, total})
curl http://localhost:8000/backfill_tree
```

The `usage_stats` object in the `/status` response contains `today`, `last_7_days`, and `lifetime` windows. Each window retains the legacy `by_model` aggregation by model name and adds `by_provider_model` for exact `provider/model` attribution. `by_scope` divides usage into six sources—`main`, `summary`, `vision`, `bubble`, `subconscious`, and `mem0`—using the same structure as the window total. Both window totals and `by_scope` include `thinking_calls`, `thinking_seconds`, and `avg_thinking_seconds`, which report average thinking time for lifecycles with a `thinking_start -> llm_response` sequence. Auxiliary summary, vision, and mem0 calls without a start event are excluded from that average. Historical logs without provider information are grouped under `unknown/<model>`. When source-level statistics are introduced during an upgrade, Coworker first rebuilds them from logs; if the raw logs have been lost, the source attribution of older aggregate data cannot be recovered.

You can also run the interactive example:

```bash
uv run python examples/api.py
```

## WebSocket

```javascript
const ws = new WebSocket("ws://localhost:8000/ws/alice");
ws.onmessage = (event) => console.log("Received:", event.data);
ws.send("Hello!");
```

Only one SSE or WebSocket long-lived connection may use the same `participant_id` at a time; the first connection wins. A later WebSocket with the same ID receives a rejection message and closes with code `1008`. A later SSE connection receives one rejection event and then ends. After the existing connection closes, the same ID can connect again.

### Direct Bubble handoff

An active Bubble bound to the same `participant_id` (and optional `conversation_id`) receives matching WebSocket or REST inbound messages and sends direct replies back through that ID's live stream. SSE is outbound-only: after subscribing to `/sse/{participant_id}`, a client sends subsequent inbound messages through `POST /messages` with the same `sender_id`; they are still handed directly to the Bubble.

To enable transparent handoff by communication participant, configure case-sensitive full-ID globs:

```env
AGENT__BUBBLE_HANDOFF_TRANSPARENCY_PARTICIPANT_MATCHES=["wecom:*","coworker-desktop:*:local:*"]
```

`*`, `?`, and `[...]` are glob wildcards; an entry without wildcards is an exact `participant_id`. These defaults make WeCom and the Desktop `local` actor transparent. Set `[]` to disable those default matches.

Every live generic WebSocket/SSE session receives the visible Bubble handoff, labeled replies, and completion notice by default. The corresponding default is:

```env
AGENT__BUBBLE_HANDOFF_TRANSPARENCY_STREAM_TRANSPORTS=["websocket","sse"]
```

List only one value to enable transparency for that transport alone, or set `[]` to disable both. Desktop identities never fall through to this generic rule: they must explicitly match a participant glob, so the defaults make only `coworker-desktop:<desktop_id>:local:…` transparent, never the `claude` or `codex` actors.

Outbound channels that support structured `extra` (generic WebSocket/SSE and Desktop) also carry provenance for transparent handoff messages under `extra.bubble`. Frontends should prefer it for handoff state instead of parsing display copy:

```json
{
  "message": "🫧 This conversation has been handed to a Bubble…",
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

Completion notices use `phase: "end"`. Direct Bubble replies use `kind: "reply"`. Plain channels without structured `extra` support, such as WeCom, do not receive this metadata and retain the `🫧 泡泡：` text prefix instead; Desktop has guaranteed support for the structured metadata, so it receives the original reply body and neither injects nor parses that prefix.

Messages, registration, SSE, and WebSocket operations for `coworker-desktop:*` participants require `Authorization: Bearer <API__COMMUNICATION_TOKEN>` in the default production mode. This check is disabled only when both the server and Desktop explicitly set `development_mode=true`; that mode is only for local debugging on a loopback address.

Browser examples:

- `examples/chat.html`
- `examples/api_test.html`

## File messages

Place message files in `data/inbox/`; the agent reads and processes them during polling. Replies are written to `data/outbox/`, and connected WebSocket users also receive a push notification.
