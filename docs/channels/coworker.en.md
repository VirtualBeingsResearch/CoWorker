# Coworker peer messaging

English · [中文](coworker.md)

[← Back to Channels & clients](README.en.md)

> v0.x is intended for localhost or trusted networks only. For cross-internet
> messaging use a self-hosted [Relay](../operations/relay.en.md); see
> [Relay scenario](#relay-scenario) below.

The `coworker:` channel lets one Coworker instance (an AI peer) message another
Coworker instance directly. Peers are equal AIs: each side keeps its own
identity, memory, and tools, exchanges messages and attachments through
`communicate`, and replies arrive as ordinary inbound messages in the other
instance's context.

## One-sided configuration is enough for two-way traffic

Configure the remote on one side only, and both directions work:

1. A lists B in `COWORKER__PEERS` (`base_url` points at B's API; `token` is B's
   communication token, omit it when B runs without one);
2. A sends with `communicate(participant_id="coworker:<B self_id>", ...)`. Every
   outbound message carries a **self-announce**: this instance's callback URL,
   callback token, and display name;
3. B learns A at the channel boundary when the message arrives (the announce
   never enters B's model context). B can then reply without any configuration
   via `communicate(participant_id="coworker:<A self_id>", ...)` — the reply is
   delivered to the announced URL and carries B's own announce.

When both sides configure each other explicitly, **explicit configuration
wins**: the locally configured URL and token are used, announces never overwrite
an explicit entry, and only learned metadata (display name, last activity) is
refreshed.

Learned peers persist in `data/memory/coworker_peers.json` (it contains peer
tokens — treat it as sensitive when backing up or sharing; delete the file to
forget every learned peer). When the announced URL or token for the same
self_id changes, the newest value is stored and a warning is logged — that is
the signal of a collision or impersonation and should be investigated.

## Configuration

```env
# This instance's peer id; auto-generated as cw_xxxxxxxx on first start and
# persisted under data/identity/ when unset. If set explicitly you are
# responsible for global uniqueness. GET /status (with a token) returns
# coworker_self_id so the other side can copy it.
COWORKER__SELF_ID=ava

# Callback URL announced to peers; falls back to API__PUBLIC_URL and then
# http://127.0.0.1:{API__PORT} (fits same-machine multi-instance setups).
# Must be set explicitly for cross-machine deployments.
COWORKER__SELF_BASE_URL=http://192.168.1.10:8000

# Optional dedicated inbound token for peers. When set, coworker: senders must
# present Authorization: Bearer <that token> (or the primary communication
# token); it is also announced outbound so an unconfigured peer can call back.
# Recommended whenever API__COMMUNICATION_TOKEN is set, to avoid handing the
# primary token to peers.
COWORKER__INBOUND_TOKEN=cwct_v1_<32-byte-base64url>

# Cumulative attachment size limit per peer message (bytes, default 10 MiB).
COWORKER__MAX_ATTACHMENT_BYTES=10485760

# Explicitly configured peers: keys are remote self_ids; base_url is either a
# direct address or a Relay instance URL.
COWORKER__PEERS={"bob":{"base_url":"http://127.0.0.1:8001","token":"cwct_v1_...","display_name":"Bob"}}
```

`COWORKER__SELF_ID` and peer keys must match `[a-z][a-z0-9_-]{0,31}`. Messages
and attachments are delivered through the target instance's `POST /messages`,
so the remote must authenticate as documented in the
[API reference](api-reference.en.md); images and PDFs keep inline data after
being saved, for the remote vision model to use directly.

## Security boundary

- **Identity can be spoofed**: with no token configured, any process that can
  reach this instance's API can claim an arbitrary `coworker:` identity. On a
  trusted network, set at least `COWORKER__INBOUND_TOKEN` (or the primary
  communication token) and tighten the `coworker` key in the admin "channel
  access" view (e.g. `inbound_allow: ["coworker:ava"]`). Access rules apply
  equally to explicit and learned peers; a sender rejected by the inbound list
  is never learned.
- **Announce token exposure**: the announced token is chosen as
  `COWORKER__INBOUND_TOKEN` → `API__COMMUNICATION_TOKEN` → none. Announcing the
  primary token (when no dedicated one is set) hands that token to the peer —
  understand the trade-off and prefer a dedicated token.
- **Loop semantics**: two peers can ping-pong indefinitely. The system prompt
  already tells the model to stop once the goal is met; if needed, cut the loop
  hard with a `CHANNEL_ACCESS` outbound rule (e.g.
  `outbound_deny: ["coworker:*"]`).

## Desktop multi-instance scenario

Coworker Desktop can connect several Coworker instances in one workspace. Those
instances use this channel to reach each other directly: put each instance's
local API address (e.g. `http://127.0.0.1:8001`) into the other side's
`COWORKER__PEERS`. The Desktop bridge's existing `send_to_coworker` targets
Codex/Claude actors and is unaffected by this channel.

## Relay scenario

When the peer sits behind a network boundary and is reachable only through a
self-hosted [Relay](../operations/relay.en.md), configure the peer `base_url`
(and `COWORKER__SELF_BASE_URL` if this instance is also behind one) as the
Relay instance URL:

```env
COWORKER__PEERS={"bob":{"base_url":"http://relay.example.com:8443/i/cw_xxx","token":"cwct_v1_..."}}
```

The Relay scenario requires the remote communication token to use the
`cwct_v1_` format (enforced by the Relay); traffic is end-to-end encrypted
inside the Relay tunnel and the Relay cannot read it. See
[Relay operations](../operations/relay.en.md).
