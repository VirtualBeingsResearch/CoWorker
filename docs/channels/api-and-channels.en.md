# API and Communication Channels

[中文](api-and-channels.md) · English

[← Back to Channels and Clients](README.en.md)

> The current v0.x releases should be used only locally or on a trusted network. Read the
> [security policy](../../SECURITY.md) before deployment.

All outbound communication is first routed by `ChannelRegistry` to an independent transport such as Stream or WeCom. Within Stream, `StreamChannel` delegates Desktop participants to the built-in Desktop profile. Coworker Desktop shares Stream Runtime registration, connections, queues, and lifecycle and uses the existing participant IDs and message protocol. `list_connections` aggregates participants that are online or otherwise reachable across channels and profiles. `/status` reports runtime, model, and usage state; `list_connections` provides connection discovery.

## Channel development model

`from coworker.channels import BaseChannel, ChannelCapabilities, ChannelRuntime, StreamProfile, create_channel_system` is the stable development entry point. `create_channel_system(outbox_dir)` is the application's single communication composition root. It returns:

- `registry`, which registers Channels, routes inbound and outbound traffic, and starts or stops each shared Runtime exactly once.
- `stream_runtime`, which owns WS/SSE connections, participant registrations, attachment storage, and offline outbox delivery and provides Stream infrastructure to HTTP and WebSocket routes.

To add an independent transport, subclass `BaseChannel` and call `channel_system.registry.register(channel)`. A Channel owns participant resolution, raw inbound normalization, and outbound semantics; mutable connection state, background tasks, and lifecycle belong to its `runtime`. For new protocol behavior over Stream, subclass `StreamProfile` and call `channel_system.register_stream_profile(profile)`. A profile owns its participant prefix, capabilities, inbound normalization, and outbound decoration while reusing `StreamRuntime`. Desktop is the built-in Stream profile. Registration boundaries report all name, prefix, base-class, Runtime, and duplicate issues in one diagnostic. `CommunicateTool` adapts model tool calls into outbound Registry requests.

The smallest outbound Channel subclasses `BaseChannel` and implements only `send`. The defaults provide a no-op Runtime, no shorthand resolution, no inbound support, an empty connection list, and activity helpers:

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

When wrapping an existing async sender, no Channel class is needed:

```python
channels.registry.register(BaseChannel.from_sender("team:", send_to_team))
```

A Channel declares support for `conversation_id`, `attachments`, and `extra` through `ChannelCapabilities`; the default accepts `message` only. Before delivery, the Registry omits unsupported optional fields. As long as a message or other supported content remains, delivery continues and the tool result tells the AI exactly which fields were not passed. Unsupported attachments or `extra` therefore never discard a valid message.

WeCom inbound events expose the frame `req_id` as `conversation_id`, falling back to `msgid` when needed. Passing that value back selects the exact reply frame. If the requested frame is missing or expired, WeCom sends an active message instead of replying through another frame from the same chat.

WeCom group messages can mention members through `extra={"mentioned_list":["userid1","userid2"]}`. The WeCom Channel converts the list to inline Markdown `<@userid>` mentions. WeCom supports `extra` but currently implements only `mentioned_list`. When a request includes other fields, the message and supported fields are still delivered, and the result lists both the unsupported fields and the currently supported field.

For inbound traffic, override `receive_raw`, normalize the payload into an `IncomingEvent`, then call `publish_inbound`. For background connections, inject a `ChannelRuntime` that implements `start` and `stop`. The Registry rejects duplicate names, duplicate participant prefixes, and late registration after startup so configuration mistakes fail during composition.

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

The `usage_stats` object in the `/status` response contains `today`, `last_7_days`, and `lifetime` windows. Each window provides both `by_model` aggregation by model name and `by_provider_model` for exact `provider/model` attribution. `by_scope` divides usage into six sources—`main`, `summary`, `vision`, `bubble`, `subconscious`, and `mem0`—using the same structure as the window total. Both window totals and `by_scope` include `thinking_calls`, `thinking_seconds`, and `avg_thinking_seconds`, which report average thinking time for lifecycles with a `thinking_start -> llm_response` sequence. Auxiliary summary, vision, and mem0 calls without a start event are excluded from that average. Historical logs without provider information are grouped under `unknown/<model>`. When source-level statistics are introduced during an upgrade, Coworker first rebuilds them from logs; if the raw logs have been lost, the source attribution of older aggregate data cannot be recovered.

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

Every live generic WebSocket/SSE session enables a transparent Bubble lifecycle by default. The takeover notice is delayed until the Bubble first receives a new message from that conversation or is about to reply directly. A matching completion notice is sent only after takeover was announced successfully; merely creating or binding a Bubble emits nothing externally. The corresponding default is:

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

An announced handoff uses `phase: "end"` when it completes. Direct Bubble replies use `kind: "reply"`. Plain channels without structured `extra` support, such as WeCom, do not receive this metadata and retain the `🫧 泡泡：` text prefix instead; Desktop has guaranteed support for the structured metadata, so it receives the original reply body and neither injects nor parses that prefix.

Messages, registration, SSE, and WebSocket operations for `coworker-desktop:*` participants require `Authorization: Bearer <API__COMMUNICATION_TOKEN>` in the default production mode. This check is disabled only when both the server and Desktop explicitly set `development_mode=true`; that mode is only for local debugging on a loopback address.

Browser examples:

- `examples/chat.html`
- `examples/api_test.html`

## File messages

Place message files in `data/inbox/`; the agent reads and processes them during polling. Replies are written to `data/outbox/`, and connected WebSocket users also receive a push notification.
