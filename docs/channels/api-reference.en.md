# API Reference

[中文](api-reference.md) · English

[← Back to Channels and Clients](README.en.md)

This page documents HTTP, SSE, and WebSocket contracts for local integrations. See
[API and Communication Channels](api-and-channels.en.md) for Channel extension and full behavior.
Coworker v0.x does not provide an enterprise multi-tenant authorization boundary. Do not expose
the API directly to the public internet.

## OpenAPI

After first-time setup, FastAPI provides:

- OpenAPI JSON: `GET /openapi.json`
- Swagger UI: `GET /docs`
- ReDoc: `GET /redoc`

Before setup is complete, ordinary routes redirect to `/admin`. The generated schema includes
management implementation endpoints. `/api/admin/*` primarily serves the matching Web management
console and is not a long-term compatibility promise for independent clients.

## Authentication scope

| Endpoint class | Default authentication |
|---|---|
| `POST /messages` | Requires a Bearer once `API__COMMUNICATION_TOKEN` is explicitly set; otherwise relies on loopback/trusted-network isolation |
| `GET /status` | Returns basic lifecycle information when a token is explicitly set but the Bearer is missing; returns the full snapshot when no token is explicitly set or with a valid Bearer |
| `GET /profile` | Requires a Bearer once a token is explicitly set; no check otherwise |
| `GET /logs/stream` | Requires a Bearer once a token is explicitly set; no check otherwise |
| Desktop participants, Desktop registration, and inner Relay requests | `Authorization: Bearer <API__COMMUNICATION_TOKEN>` (administrator-token fallback when not explicitly set) |
| `/api/admin/*` and configuration export | Administrator token |
| Desktop release management | Desktop-update administrator token or administrator token |

Communication Bearer checks for ordinary REST messages, full status snapshots, the identity
profile, and the runtime log stream apply only after `API__COMMUNICATION_TOKEN` is explicitly
set; without it those endpoints keep their previous behavior. Desktop communication falls back to
the administrator token when no dedicated token is explicitly set. Set
`API__COMMUNICATION_TOKEN` for long-running use.

## Core HTTP endpoints

| Method and path | Purpose |
|---|---|
| `POST /messages` | Queue a message and optional attachments for the Agent |
| `GET /status` | Runtime, model, and usage snapshot |
| `GET /profile` | Identity, profile text, and earliest log time |
| `POST /switch_model` | Switch the main Provider/model |
| `GET/PATCH /model_config` | Read or change summary, fallback, and vision settings |
| `GET/POST /backfill_tree` | Query or start historical memory-tree backfill |
| `GET /backups` | List emergency short-term-context backups |
| `POST /backups/restore` | Restore an emergency backup in `full` or `summarize` mode |
| `GET /api/debug/tasks` | Event-loop diagnostics for trusted environments only |

### Send a message

When a communication token is configured (`API__COMMUNICATION_TOKEN`, falling back to the
administrator token), every `POST /messages` request must include:

```text
Authorization: Bearer <API__COMMUNICATION_TOKEN>
```

```json
{
  "sender_id": "integration:alice",
  "content": "Summarize today's tasks",
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

An ordinary accepted message returns:

```json
{
  "status": "queued",
  "sender_id": "integration:alice",
  "conversation_id": "daily"
}
```

`sender_id` contributes to the persistent conversation-isolation boundary. Keep it stable,
auditable, and unique to the participant. Attachment `data` is Base64. HTTP success means queued,
not that a model response has completed. If the channel's inbound access lists reject `sender_id`,
the server returns `403` before decoding attachments or queuing the message.

### Status and models

```bash
# When a token is configured: no Bearer returns basic status; a valid token returns the full snapshot
curl http://127.0.0.1:8000/status \
  -H "Authorization: Bearer <API__COMMUNICATION_TOKEN>"

curl -X POST http://127.0.0.1:8000/switch_model \
  -H "Content-Type: application/json" \
  -d '{"provider":"deepseek","model_id":"deepseek-chat"}'
```

When an administrator has configured a communication token, unauthenticated `/status` returns
`status`, `is_running`, `is_sleeping`, `setup_mode`, `communication_token_configured`, and
`authenticated`; it never includes providers, model configuration, or usage. When no token is
configured or a valid Bearer is supplied, the endpoint returns the full snapshot. The endpoint may
gain fields when optional modules are available. Clients should tolerate unknown fields.
Integrations that require an audit trail should retain the raw response and Coworker version.

## SSE and WebSocket

- WebSocket: `ws://127.0.0.1:8000/ws/{participant_id}` for bidirectional text.
- SSE: `GET /sse/{participant_id}` for outbound messages; send inbound messages through
  `POST /messages` with the same ID.
- Only one SSE or WebSocket connection may use a `participant_id` at a time.
- SSE sends a comment heartbeat every 15 seconds; proxies should disable response buffering.
- `coworker-desktop:*` IDs require a communication Bearer. Native browser `EventSource` cannot set
  an Authorization header, so do not use it for a protected Desktop participant.
- The runtime log stream `GET /logs/stream` also requires a Bearer once a communication token is
  explicitly set; the identity page consumes it through an authenticated fetch stream.

Outbound events contain message text and may include structured `extra`, such as Bubble handoff
state. Prefer `extra.bubble` over parsing localized notice text.

## Errors and retries

FastAPI errors usually have this form:

```json
{"detail":"error description"}
```

| Status | Response |
|---|---|
| `400/422` | Correct request, model, or protocol fields; do not retry unchanged |
| `401/403` | Check token, authentication scope, and channel access lists; never log the full Authorization value |
| `404` | Check resource, version, or whether the feature is enabled |
| `409` | A task or connection already exists; query its state first |
| `503` | Agent, Channel, or token is not ready; retry with backoff |

Ordinary `/messages` has no general idempotency key. Only the Desktop protocol uses `message_id`
for bounded deduplication. Custom integrations should avoid repeating side-effecting messages.

## Management and release endpoints

`/api/admin/*`, `/api/desktop-updates/*`, and `/api/export_config` can change configuration,
restore state, publish artifacts, or export secrets. Unless you are developing the matching
official console, prefer the Web UI and read [Web Management Console](../guides/README.en.md) and
[Data and Trust Boundaries](../architecture/data-boundaries.en.md) first.

[← Back to project home](../../README.en.md)
