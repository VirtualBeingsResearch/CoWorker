# API and Communication Channels

[中文](api-and-channels.md) · English

[← Back to Channels and Clients](README.en.md)

> The current v0.x releases should be used only locally or on a trusted network. Read the
> [security policy](../../SECURITY.en.md) before deployment.

All outbound communication is first routed by `ChannelRegistry` to an independent transport such as Stream, WeCom, or Weixin Claw. Within Stream, `StreamChannel` delegates Desktop participants to the built-in Desktop profile. Coworker Desktop shares Stream Runtime registration, connections, queues, and lifecycle and uses the existing participant IDs and message protocol. `list_connections` aggregates participants that are online or otherwise reachable across channels and profiles. `/status` reports runtime, model, and usage state; `list_connections` provides connection discovery.

When sending through the built-in Stream, Desktop, WeCom, or Weixin Claw channels, `communicate` accepts only complete participant IDs present in `list_connections` (an exact shorthand explicitly supported by a channel remains valid). An unknown ID is never corrected automatically and no message is sent. If it is within an edit distance of four characters from a known ID, the tool lists similar complete IDs for the model to choose from; otherwise it treats the ID as nonexistent and asks the model to call `list_connections` again. A registered Stream participant remains known while offline and can still receive outbox delivery.

## Channel development model

`from coworker.channels import BaseChannel, ChannelAccessController, ChannelActivityStore, ChannelCapabilities, ChannelRuntime, ChannelModule, ChannelManagement, ChannelSettings, StreamProfile, create_channel_system` is the stable development entry point. `create_channel_system(outbox_dir, activity_path=None, access_config=None)` is the application's single communication composition root. It returns:

- `registry`, which registers Channels, routes inbound and outbound traffic, and starts or stops each shared Runtime exactly once.
- `stream_runtime`, which owns WS/SSE connections, participant registrations, attachment storage, and offline outbox delivery and provides Stream infrastructure to HTTP and WebSocket routes.
- `activity`, which records each participant's latest successful send and receive times. When `activity_path` is provided, atomic JSON persistence restores them after an application restart.
- `access`, the shared inbound/outbound participant-access controller used by the Registry, Channels, and Stream profiles.
- `modules`, which stores management interfaces and hot-settings providers contributed by complete channel modules.

To add an independent transport, subclass `BaseChannel`. A transport-only integration may call
`channel_system.registry.register(channel)`. When it also owns connection management or hot settings,
implement `ChannelModule` and call `channel_system.install(module)` to register the transport, optional
`ChannelManagement`, and optional `ChannelSettings` together. Admin only routes snapshots and commands
through `/api/admin/channels/{channel}/management`; hot configuration iterates the modules' declared
`config_key` values. Neither generic layer interprets channel-private semantics. A Channel owns participant resolution, raw inbound normalization, and outbound semantics; mutable connection state, background tasks, and lifecycle belong to its `runtime`. For new protocol behavior over Stream, subclass `StreamProfile` and call `channel_system.register_stream_profile(profile)`. A profile owns its participant prefix, capabilities, inbound normalization, and outbound decoration while reusing `StreamRuntime`. Desktop is the built-in Stream profile. Registration boundaries report all name, prefix, base-class, Runtime, and duplicate issues in one diagnostic. `CommunicateTool` adapts model tool calls into outbound Registry requests.

A Channel may override `agent_instructions()` to teach the agent stable channel operations. The Registry only aggregates text contributed by enabled channels, and `SystemPromptBuilder` places it in a cache-stable `[CHANNELS]` section. Do not inject dynamic connection lists or polling state into the system prompt. Live participants remain discoverable through `list_connections`; interpretation and execution of channel-private `extra` structures stay inside the destination Channel, and the Registry does not inspect them.

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

The built-in Stream, Desktop, and WeCom implementations share `channels.activity`. A custom Channel that wants `list_connections` activity to survive restarts can receive `activity=channels.activity` and call `record_received` / `_record_sent` only after accepting inbound traffic or completing outbound delivery; failed attempts do not advance activity timestamps.

A Channel declares support for `conversation_id`, `attachments`, and `extra` through `ChannelCapabilities`; the default accepts `message` only. Before delivery, the Registry omits unsupported optional fields. As long as a message or other supported content remains, delivery continues and the tool result tells the AI exactly which fields were not passed. Unsupported attachments or `extra` therefore never discard a valid message.

## Channel access lists

`CHANNEL_ACCESS` configures inbound and outbound participant allowlists and denylists by channel. The same settings can be changed in **Channel Access** in the administration console and take effect immediately:

```env
CHANNEL_ACCESS={"wecom":{"inbound_allow":["wecom:trusted:*"],"inbound_deny":["wecom:trusted:blocked"],"outbound_allow":[],"outbound_deny":["wecom:external:*"]},"desktop":{"inbound_allow":[],"inbound_deny":[],"outbound_allow":["coworker-desktop:*:local:*"],"outbound_deny":[]}}
```

Each channel has four lists: `inbound_allow`, `inbound_deny`, `outbound_allow`, and `outbound_deny`. Patterns match the complete participant ID case-sensitively and support `*`, `?`, and `[...]`; a value without wildcards is exact. Evaluation is: a matching deny rejects; otherwise a non-empty allow requires a match; otherwise access is allowed. An unconfigured channel, `{}`, or four empty lists therefore preserves the compatible allow-all behavior.

Built-in configuration keys are `stream`, `desktop`, `wecom`, and `weixin`. A Stream profile uses its own channel name, so Desktop participants are governed by `desktop`, not `stream`. Extension Channels use their registered names. Inbound rejection happens before attachment download, reply-frame/context-token caching, activity recording, and Agent handling. REST `/messages` returns `403`, WebSocket closes with `1008`, and WeCom or Weixin Claw silently drops the event while logging no message body. The Registry enforces outbound rejection, and denied participants are hidden from the Agent's `list_connections` while their rules remain editable by administrators.

These lists answer only whether a canonical participant address is allowed in one channel direction. They are not authentication, tenant isolation, or a policy for who may wake the Agent. Aggregate participants such as groups and bot instances are evaluated by their own participant IDs and are not resolved to real-person identities.

WeCom direct messages do not expose a `conversation_id`; replies automatically use the user's latest fresh frame. Group-chat events expose the frame `req_id` as `conversation_id`, falling back to `msgid` when needed. Passing that value back selects the exact reply frame. If the requested frame is missing or expired, WeCom sends an active message instead of replying through another frame from the same group. A group send without `conversation_id` is also always proactive and never uses a cached frame automatically.

WeCom AI Bots currently do not support mentioning group members through the API, so the WeCom Channel does not provide member mentions.

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
AGENT__BUBBLE_HANDOFF_TRANSPARENCY_PARTICIPANT_MATCHES=["wecom:*","weixin:*","coworker-desktop:*:local:*"]
```

`*`, `?`, and `[...]` are glob wildcards; an entry without wildcards is an exact `participant_id`. These defaults make WeCom, Weixin Claw, and the Desktop `local` actor transparent. Historical saved copies of the old default list evolve with the product defaults; custom lists, including an explicit `[]`, remain unchanged.

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

An announced handoff uses `phase: "end"` when it completes. Direct Bubble replies use `kind: "reply"`. Plain channels without structured `extra` support, such as WeCom and Weixin Claw, do not receive this metadata and retain textual takeover/completion notices plus the `🫧 泡泡：` reply prefix; Desktop has guaranteed support for the structured metadata, so it receives the original reply body and neither injects nor parses that prefix.

Messages, registration, SSE, and WebSocket operations for `coworker-desktop:*` participants require `Authorization: Bearer <API__COMMUNICATION_TOKEN>` in the default production mode. When no dedicated communication token is configured, the server falls back to the administrator token for a smoother first local connection; configure a dedicated token when the permissions must be isolated. This check is disabled only when both the server and Desktop explicitly set `development_mode=true`; that mode is only for local debugging on a loopback address.

Browser examples:

- `examples/chat.html`
- `examples/api_test.html`

## File messages

Place message files in `data/inbox/`; the agent reads and processes them during polling. Replies are written to `data/outbox/`, and connected WebSocket users also receive a push notification.
