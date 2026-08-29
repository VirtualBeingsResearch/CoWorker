# Model API (OpenAI-compatible)

中文 · [English](model-api.md)

[← Back to Communication & clients](README.en.md)

The model API exposes Coworker as an **OpenAI Chat Completions-compatible "model"**: anything that can talk to a chat model — chat clients, IDE extensions, automation scripts — can reach the partner with a standard `base_url + api_key`. Callers believe they are calling a model; they are actually talking to a partner with memory, tools, and continuous progress reporting.

## Enabling

The model API is disabled by default. Configure at least one token in `.env`:

```bash
MODEL_API__ENABLED=true
MODEL_API__TOKENS='[{"token":"sk-my-long-token","display_name":"Alice"}]'
```

Each token maps to one participant:

- With `display_name` configured, the participant is `api:<lowercased-hyphenated-name>` (e.g. `api:alice`);
- Otherwise it falls back to `api:<8 hex chars>` derived from a hash of the token.

Different tokens = different participants, attached directly to the existing Persona (contact book) system: the first request automatically creates a `Person` and binds the alias, so the agent sees the person card from the very first message. All `/v1` requests must carry `Authorization: Bearer <token>`; mismatched tokens get 401, and a disabled feature or not-ready agent gets 503.

## Protocol semantics

Callers use the standard OpenAI protocol; Coworker adds a few conventions:

### One request = one conversation turn

One `/v1/chat/completions` request opens one conversation turn. While working, the agent **sends multiple replies** (status, findings, partial results); each appears in the response stream immediately — the partner reports progress like a person would, instead of staying silent and returning everything at once.

### Ending a turn: `end_turn`

By default the request stays open until the agent considers the turn finished. The agent ends the turn by sending its last `communicate` with `extra={"end_turn": true}`; the response closes with `finish_reason: "stop"` and a custom `coworker_end_reason: "end_turn"` field on the final chunk (streaming) or response object (non-streaming).

### Caller tools: `tool_calls`

The request's `tools` schemas, like the system prompt, are handed to the model as **scenario context** for it to interpret (they are not registered as internal agent tools). When the model wants to invoke a tool exposed by the caller's app, it replies with `extra={"tool_calls": [...]}` (OpenAI format); the response ends with `finish_reason: "tool_calls"` and `coworker_end_reason: "tool_calls"`. After executing the tools, the caller sends the `role: "tool"` results back in its next request, and the conversation continues.

### Caller system prompt: scenario, not instructions

The caller's own system prompt is injected into the agent context inside a "[caller scenario]" block, treated **as background information only** — it never overrides the agent's own identity or safety boundaries. Scenarios are hash-deduplicated: an unchanged scenario is not re-injected within the same conversation.

## Conversation stickiness

The OpenAI protocol has no conversation id, so Coworker assigns one via **history fingerprint matching**: OpenAI-compatible clients resend the full history every request, the server fingerprints each message, and the largest "request head ≖ known history tail" overlap identifies the conversation (tolerating clients that trim old messages with a sliding window); the server generates the conversation id. A request matching nothing starts a new conversation. Inbound events carry a `[conversation:...]` label; the agent keeps that conversation id when replying so the reply reaches the right request.

A second concurrent request on the same conversation is never queued or rejected: the new messages are **delivered directly** into the running conversation as follow-up input, and the new request attaches to the same output stream; `end_turn` closes all attached requests together. Serializing requests is the client's own choice.

## Lifecycle

Timers measure "time with no output", so an agent that keeps reporting never trips them:

- **5 minutes with no output**: a system reminder is injected into the agent, asking it to report progress or finish the turn;
- **20 minutes with still no output**: the agent is told the turn's HTTP response has been closed, and the stream is disconnected (final chunk carries `coworker_end_reason: "timeout"`). The conversation itself survives; the client can pick it back up with its history on the next request.

Tune the thresholds with `MODEL_API__NUDGE_SECONDS` (default 300) and `MODEL_API__TIMEOUT_SECONDS` (default 1200).

A not-ready agent or disabled feature returns 503; when every upstream model candidate fails, the agent loop's fallback chain exhausting also surfaces as 503 / an in-stream error. Non-streaming requests return the concatenation of every reply in the turn.

## Capabilities and limitations

- `GET /v1/models` returns a single model, `coworker`; any `model` value in requests is accepted and echoed back.
- `usage` numbers are local estimates, not exact upstream metering.
- Multimodal messages contribute their text parts only; parameters like `n>1` and `logprobs` are ignored.
- Scenario (system prompt + tools) injection has a length budget (`MODEL_API__SCENARIO_MAX_CHARS`, default 6000); overflow is truncated and marked.
- The model API is currently not added to the Relay public-tunnel whitelist; for public access use a reverse proxy that terminates TLS.
- Future directions: per-conversation concurrent execution units (generalized bubbles), and presence-awareness injection so the agent coordinates shared-resource conflicts itself.
