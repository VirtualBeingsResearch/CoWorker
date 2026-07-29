# Self-hosted Relay

English · [中文](relay.md)

Coworker Relay lets a Coworker on a private network establish an outbound connection and lets a
new Desktop reach status, registration, messaging, SSE, and desktop updates through one public
endpoint:

```text
http://relay.example.com:8443/i/{instance_id}
```

The outer endpoint may use plain HTTP/WebSocket because Desktop and Coworker establish a
public-key-pinned TLS 1.3 connection inside the relayed byte stream. Relay can observe source IP,
instance, connection times, sizes, and timing, but cannot read or forge tokens, paths, headers,
messages, attachments, SSE events, or update artifacts. Availability still depends on Relay,
which can drop, delay, or rate-limit traffic.

Older Desktop versions do not support this protocol and must continue connecting directly to
Coworker. Relay has no legacy HTTP proxy facade; ordinary paths such as
`/i/{instance_id}/status` return `404`.

## Initialization and deployment

`apps/coworker-relay/` provides one `coworker-relay` service and administration tool. Releases
include a Relay image and platform binaries. On the first `init`, the wizard asks whether to use
a container.

For container deployment (the default), run:

```bash
coworker-relay init
cd coworker-relay-deploy
docker compose up -d
```

For native deployment, the generated configuration uses host data paths and binds the
administration listener only to loopback:

```bash
cd coworker-relay-deploy
coworker-relay serve
```

The wizard shows a public-origin example and defaults to `http://<host>:8443`. It does not require
a domain, certificate, ACME, or public port 80. Use `--deployment container|native` for
non-interactive initialization:

```bash
coworker-relay init \
  --public-url http://203.0.113.10:8443 \
  --deployment native
```

`coworker-relay --help` and each subcommand's `--help` are self-contained. Initialization writes
a mode-`0600` `.env` containing a random administrator token. The service and CLI read `.env`
from the current directory by default; `--config` and `RELAY_CONFIG` select another file.
Existing files are never silently overwritten.

Native mode stores the database and Relay signing key under `data/` in the deployment directory.
Compose mode publishes public port `8443` and binds the administration port only on the host
loopback:

```text
0.0.0.0:8443 -> relay:8443
127.0.0.1:8444 -> relay:8444
```

Never expose the administration port publicly. Operators should SSH to the Relay host and run the
CLI there. The minimum configuration is:

```text
RELAY_PUBLIC_URL=http://relay.example.com:8443
RELAY_LISTEN=:8443
RELAY_ADMIN_LISTEN=:8444
RELAY_DATABASE=/var/lib/coworker-relay/relay.db
RELAY_SIGNING_KEY=/var/lib/coworker-relay/relay-signing.key
RELAY_ADMIN_TOKEN=<random value of at least 24 characters>
```

An HTTPS/WSS reverse proxy may be placed in front. In that case, set `RELAY_PUBLIC_URL` to the
public HTTPS origin and enable WebSocket forwarding. Inner end-to-end encryption remains enabled.
Only proxies in `RELAY_TRUSTED_PROXY_CIDRS` may supply the source IP.

## Pairing and Desktop

From the deployment directory, create a single-use pairing code valid for ten minutes:

```bash
coworker-relay instance create --name home-coworker
```

Enter the Relay URL and pairing code on Coworker's Remote Access page. Pairing uses a
challenge-HMAC, so the pairing code is never sent in plaintext. Coworker generates an instance
key and pins Relay's signing key. Then copy the displayed Base URL and current communication
token to a new Desktop:

```text
Base URL: http://relay.example.com:8443/i/cw_xxx
Bearer Token: cwct_v1_...
```

Desktop needs no transport, certificate, or public-key fields. It recognizes the exact instance
path, displays “Relay / End-to-end encrypted,” and never falls back to plaintext HTTP after
connection, identity, or protocol failures.

Relay requires communication tokens in `cwct_v1_<32-byte-base64url>` format. Enabling Relay
generates and securely persists one when no token exists. An existing weak-format token requires
an explicit rotation so direct Desktop configurations are not silently broken. Every Desktop
must be updated after rotation.

“Test connection” authenticates a real pairing or control connection; it does not request a public
`/status`.

## Administration, bans, and backup

```bash
coworker-relay health
coworker-relay version
coworker-relay instance list
coworker-relay instance revoke cw_xxx
coworker-relay bans list --instance cw_xxx
coworker-relay bans remove --instance cw_xxx --ip 203.0.113.8 --reason "false positive"
coworker-relay metrics
coworker-relay gc
coworker-relay backup --output relay-backup.db
```

Relay counts invalid entry signatures by `instance_id + source IP`. Five failures in ten minutes
produce a persistent one-hour ban. Removing a ban requires an audit reason. Connection-rate,
frame-size, and global signature-verification limits run before expensive verification. Relay
only forwards bounded binary chunks and cannot interpret inner requests or routes.

Back up the database, `.env`, and Relay signing key before upgrading. When the database schema is
not E2EE Relay v1, startup stops with backup, removal, and reinitialization instructions instead of
guessing a migration.

Relay v1 is single-node. Do not share one bbolt volume among replicas or randomly load-balance an
instance across replicas. SIGTERM stops new connections, closes tunnels, and performs a bounded
shutdown.

## Data and security boundary

- Public endpoints are limited to `GET /healthz`, pairing WebSocket, Coworker control WebSocket,
  and the per-instance Desktop WebSocket.
- Relay persists instance public keys, authentication epochs, pairing state, source-IP bans,
  audit events, and aggregate traffic counters. It does not cache updates or business content.
- Raw tokens, Authorization, request paths, headers, bodies, messages, attachments, and update
  content must not appear in Relay logs, databases, metrics, errors, or crash output.
- After decryption, Coworker exposes only Desktop communication and read-only update routes.
  Administration, models, logs, backups, release management, and arbitrary HTTP/TCP proxying stay
  inaccessible.
- The original Bearer remains inside the encrypted request and is still authenticated by
  Coworker's existing endpoint authentication.
- Update checks and artifacts also use end-to-end encryption; Tauri updater still verifies the
  release signature.
- v1 does not provide P2P, WebRTC, WireGuard, offline messaging, per-device authorization, or
  multi-node high availability.

See [Relay v1 protocol](relay-protocol.en.md) for byte formats, key domains, and compatibility.
