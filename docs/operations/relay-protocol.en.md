# Relay v1 protocol and compatibility boundary

English · [中文](relay-protocol.md)

[← Back to Relay operations](relay.en.md)

Go Relay, Python Coworker, and Rust Desktop use protocol version `1`. A version, identity, or
signature mismatch fails closed and never falls back to plaintext HTTP.

## Public endpoints and pairing

```text
GET /healthz
WS  /_relay/v1/pair
WS  /_relay/v1/coworker
WS  /i/{instance_id}/_relay/v1/connect
```

A pairing code contains 32 random bytes, expires after ten minutes, and can be atomically consumed
only once. Coworker computes an HMAC over a fixed byte encoding of Relay's nonce, pairing ID,
and instance public key. Relay returns its static signing public key, `instance_id`, and a
binding signature. Later control connections use the instance Ed25519 signature, a random nonce,
connection ID, and monotonic authentication epoch to reject replay, reordering, and cross-instance
frames.

## Token derivation

The communication token must be `cwct_v1_` plus 32 unpadded base64url bytes. HKDF-SHA256 uses the
`coworker-relay-e2ee-v1` domain, an instance-bound salt, and separate purposes:

- `relay-entry-auth` for Relay entry-challenge signatures;
- `inner-tls-server` for Coworker's inner TLS identity;
- `inner-client-proof` for Desktop's inner client proof.

Coworker synchronizes only the entry public key and authentication epoch. Relay receives neither
the token nor either inner private key. Entry challenges bind version, instance, epoch, connection
ID, random nonce, and expiration.

## Byte relay and inner TLS

After entry authentication, Relay assigns a 16-byte session ID and sends a signed session-open on
Coworker's control channel. Every binary Desktop WebSocket message is an opaque TLS byte chunk.
Control-channel data adds only the session ID prefix. Relay limits outer frame size, count, queues,
and connection time but cannot observe inner stream counts or routes.

Desktop and Coworker establish TLS 1.3 inside that byte stream. Desktop accepts only the
token-derived Coworker Ed25519 certificate public key. After the handshake, Coworker sends a
one-time challenge signed by Desktop's independent `inner-client-proof` key. Virtual requests are
accepted only after both steps, so Relay cannot reuse entry authentication to impersonate Desktop.
Signatures cover fixed-order raw bytes and never depend on JSON reserialization.

## Virtual HTTP

The inner protocol has a fixed ten-byte header:

```text
version:u8 | type:u8 | stream_id:u32be | payload_length:u32be
```

Frames cover client proof, request start/body/end, response start/body/end, cancellation, errors,
and ping. Headers are ordered `[name, value]` arrays that retain duplicate order. Bodies and SSE
events stream in chunks. Stream IDs permit concurrency; receivers enforce bounded queues,
backpressure, frame-size, and header limits.

After decryption, Coworker applies one Relay exposure policy covering status, Desktop registration,
messages, SSE, and published desktop updates. Existing ASGI authentication still verifies the
original Bearer.

## Source context

Relay signs source IP, public origin, instance, and session in session-open. From this trusted
context and the decrypted original target, Coworker appends `X-Coworker-Relay-*`, `Forwarded`,
Original URL/Target, and Request ID. Client-provided duplicates remain first, and the trusted
boundary is stored in `scope.state.coworker_relay`. Authentication, authorization, and source
decisions must use trusted context rather than similarly named client headers.

## Compatibility commitment

- v1 may add control fields that receivers explicitly ignore, but cannot change signature input,
  key purposes, or frame semantics.
- New frame types, authentication semantics, or key derivation require a new version or explicit
  capability negotiation.
- Relay databases carry an explicit schema; non-E2EE-v1 schemas stop startup and require reinitialization.
- Relay and Coworker should be upgraded together. Only new Desktop versions support this protocol.
