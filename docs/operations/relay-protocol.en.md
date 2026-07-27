# Relay v1 Protocol and Compatibility Boundary

[中文](relay-protocol.md) · English

[← Back to Relay operations](relay.en.md)

This document is the v1 contract between Go Relay and Coworker's built-in Python Relay Client.
Both sides must send protocol version `1`. Relay returns `426 protocol_incompatible` for a
mismatch and does not silently downgrade.

## Connection and frames

Coworker opens `wss://<relay>/_relay/v1/connect` with its long-lived instance credential. The
tunnel uses UTF-8 JSON text frames:

- `ping`, `pong`: connection liveness.
- `verifier`, `verifier_ack`: atomic synchronization of the communication token's Argon2id
  verifier.
- `request`: an HTTP request that passed Relay's edge authentication.
- `response_start`, `response_body`, `response_error`: streaming responses.
- `cancel`: cancellation when the public request disconnects, times out, or exceeds backpressure.

`request_id` correlates a request and response within one tunnel connection. Relay does not replay
ordinary business requests after a disconnect and provides no offline outbox. This avoids
duplicating a message when the original result is unknown. Existing Desktop and Coworker protocols
retain their own ACK and deduplication semantics.

## Headers and source context

The request frame represents `headers` as an array of `[name, value]` pairs.
`relay_header_start` identifies the first Relay-appended header, allowing Coworker to distinguish
client values from trusted Relay additions.

Go's `net/http` preserves multiple values for one header name but does not expose the original
global wire order across different names. Relay therefore sorts client headers deterministically
by name, preserves value order within each name, and appends Relay headers afterwards.
Authentication, authorization, and source-IP decisions may use only the authenticated tunnel
context and appended region, never client-supplied duplicates.

## Streaming, limits, and backpressure

- v1 buffers the complete request body and puts it in one Base64 `request` frame, with a 32 MiB
  limit.
- Tunnel text frames are limited to 48 MiB to allow Base64 and protocol overhead.
- Response bodies use multiple `response_body` frames, including SSE and update downloads.
- Each response stream has a bounded buffer. Relay cancels a slow stream instead of consuming
  unbounded memory.
- Relay sends `cancel` when a client disconnects, but cancellation is best effort and is not a
  transaction rollback.

Truly streaming request uploads, resumable request delivery, and generic file tunnels are outside
v1.

## Update cache

In v1, Coworker's existing read-only update endpoints still produce update responses. Relay caches
only allowlisted installer paths that return `200`, and authentication runs again before every
cache hit. A client cannot submit an arbitrary upstream URL, and Relay is not a general upstream
downloader.

Cached content is checked with SHA-256 and supports ETag and Range reads. Desktop independently
verifies Tauri update signatures; cache integrity does not replace release signing.

## Compatibility commitment

- Protocol `1` may add response JSON fields that receivers ignore, but existing field meanings
  cannot change.
- New frame types, chunked request uploads, or authentication-semantic changes require a new
  protocol version or capability negotiation.
- Upgrade Relay and Coworker together. `relayctl health` reports Relay's build and protocol
  versions.
- v1 is single-node. An instance has one active tunnel; a new connection supersedes the old one.
