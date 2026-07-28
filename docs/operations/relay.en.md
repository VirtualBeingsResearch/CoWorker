# Self-hosted Relay

[中文](relay.md) · English

Coworker Relay lets a Coworker inside a private network open an encrypted outbound connection and expose Desktop status, registration, messaging, SSE, and update endpoints through one path:

```text
https://relay.example.com/i/{instance_id}
```

Relay terminates public HTTPS and can see headers and bodies while processing a request, but it does not persist message bodies, attachments, or SSE events.

## Deployment

The Go service and management CLI live in `apps/coworker-relay/`:

```bash
docker pull ghcr.io/virtualbeingsresearch/coworker-relay:VERSION
relayctl init --public-url https://relay.example.com:8443
cd coworker-relay-deploy
docker compose up -d
```

Each Coworker release publishes a multi-platform `linux/amd64` and `linux/arm64` Relay image on
GHCR, plus Linux, macOS, and Windows archives containing `coworker-relay` and `relayctl`,
SHA-256 checksums, and build provenance. Pin a version tag in production. For source development,
continue to use `docker build -t coworker-relay .` and `go build ./cmd/...` under
`apps/coworker-relay/`.

`relayctl init` creates a deployment `.env` (mode `0600`), `compose.yaml`, and
`.gitignore`; persistent state uses a dedicated Docker volume. It generates and displays a random
administrator token once, defaults to external port 8443, and refuses to
replace existing generated files unless `--force` is explicitly supplied.
For a DNS name, ACME is the default. Use `--acme-domain <domain>` to select the
certificate name, or `--tls-cert <path> --tls-key <path>` for PEM files,
private CAs, and public IP addresses. `relayctl` automatically reads `.env` in
its working directory; `RELAY_CONFIG` selects another file. For a private CA,
set `RELAY_CA_CERT` to its PEM bundle. This extends normal trust validation;
there is no insecure skip-verification mode.

The generated container runs without root privileges. In ACME mode Compose maps
host port 80 to container port 8080 and stores ACME state in the Relay data
volume.

Minimum configuration:

```text
RELAY_PUBLIC_URL=https://relay.example.com
RELAY_ADMIN_TOKEN=<random administrator token with at least 24 characters>
RELAY_DATABASE=/var/lib/coworker-relay/relay.db
RELAY_LISTEN=:8443
```

For container secrets, use `RELAY_ADMIN_TOKEN_FILE=/run/secrets/relay-admin-token` instead of
putting `RELAY_ADMIN_TOKEN` directly in the environment. The two settings are mutually exclusive.

The example Compose file publishes container port `8443` as host port `8443`.
Set `RELAY_PUBLIC_URL=https://relay.example.com:8443` when connecting to it
directly. If a reverse proxy publishes standard port 443, set the public URL to
`https://relay.example.com` and forward that proxy to the container's port 8443.

Use external PEM files (`RELAY_TLS_CERT`, `RELAY_TLS_KEY`) or domain ACME (`RELAY_ACME_DOMAIN`, optional `RELAY_ACME_EMAIL`). Public-IP and private-CA certificates use the PEM mode. ACME also requires `RELAY_ACME_HTTP_LISTEN`, which defaults to `:80`.

When another reverse proxy is in front, set `RELAY_TRUSTED_PROXY_CIDRS`. Forwarded addresses from other peers are not used as ban identities.

## Pairing

```bash
export RELAY_URL=https://relay.example.com
export RELAY_ADMIN_TOKEN='<administrator token>'
relayctl instance create --name home-coworker
```

When running from the generated deployment directory, the two exports are not
needed because `relayctl` reads its `.env`.

Enter the one-time code on Coworker's Remote access page. It expires after ten minutes and works once. After pairing, copy the displayed Base URL and Bearer Token into Desktop. Test remote connection sends the current communication token to the public instance's `/status`, covering public HTTPS, Relay pre-authentication, the active tunnel, and the Coworker response.

## Administration

```bash
relayctl health
relayctl version
relayctl instance list
relayctl instance update-auth cw_xxx optional
relayctl instance update-auth cw_xxx required
relayctl instance update-stats cw_xxx
relayctl bans list --instance cw_xxx
relayctl bans remove --instance cw_xxx --ip 203.0.113.8 --reason "false positive"
relayctl cache inspect
relayctl metrics
relayctl gc
relayctl instance revoke cw_xxx
```

“Rotate instance credential” first stages only the new credential digest at Relay. The old
credential remains valid until Coworker persists the new credential and successfully authenticates
a WSS connection with it, at which point Relay promotes it atomically. A lost response or
mid-rotation disconnect cannot lock out the instance.
`relayctl instance rotate-credential cw_xxx` is available for emergency
administration, but its returned credential must be written to that Coworker or the tunnel remains
offline. The administrator token is one shared privileged credential; v1 has no multi-admin RBAC.
Distribute it through a secret manager and restrict CLI hosts.

To rotate the administrator token, generate at least 32 random bytes, update
`RELAY_ADMIN_TOKEN` in the deployment `.env`, and run
`docker compose up -d --force-recreate relay`. The old token becomes invalid when the new process
starts. Back up the database first and update CLI configuration through a secure channel.

Desktop updates start in `optional` mode so older builds can update anonymously. New builds attach the Coworker Bearer to update checks and package downloads. Switch to `required` after older clients have migrated.

## Backup, upgrade, and restore

Create a consistent online bbolt snapshot before upgrading:

```bash
relayctl backup --output relay-before-upgrade.db
docker compose pull
docker compose up -d
relayctl health
```

Restore only while Relay is stopped. `--force` preserves the previous database under a timestamped
`before-restore` name instead of deleting it:

```bash
docker compose stop relay
relayctl restore \
  --from relay-before-upgrade.db \
  --database /path/to/mounted/relay.db \
  --force
docker compose start relay
```

The database has an explicit schema version. Relay refuses a newer or unsupported schema instead
of guessing a downgrade. Treat database backups, `.env`, ACME state, and Coworker's local instance
credential as secrets; encrypt them and test restoration. `relayctl gc` immediately removes
expired pairing, failure, and ban records, and Relay also performs hourly collection. Revoking an
instance cascades through its security state, statistics, and cache.

On SIGTERM, Relay stops accepting work, closes tunnels, and gives HTTP requests up to 30 seconds
to drain. Compose grants a 35-second stop grace period.

## Health, metrics, and logs

- `/_relay/v1/livez` reports process liveness.
- `/_relay/v1/readyz` and the compatible `/_relay/v1/health` check draining state, database and
  cache availability, and report build and protocol versions.
- `relayctl metrics` returns administrator-Bearer-protected Prometheus text for requests,
  authentication failures, bans, latency, Argon2 concurrency, tunnel connections, online tunnels,
  and cache capacity/hits.

Relay writes structured JSON logs to stdout. Security logs contain a full source IP, instance,
route category, authentication result, and Request ID, but no token, cookie, body, attachment, or
complete original URL. Configure rotation and retention in the container runtime and treat source
IPs as personal data. Alert on readiness failures, authentication spikes, reconnect storms, cache
quota pressure, and certificate expiry.

## Capacity and topology boundary

v1 is single-node: bbolt uses a local volume and tunnel ownership lives in process memory. Never
mount one data volume into multiple Relay replicas. Cold-backup recovery is supported;
active-active and zero-downtime rolling upgrades are not.

Public requests default to 600 requests per minute per instance and source IP, while anonymous
requests default to 60. Argon2 verification has a global concurrency bound. Tune policy with
`RELAY_REQUESTS_PER_MINUTE`, `RELAY_ANONYMOUS_PER_MINUTE`,
`RELAY_VERIFIER_CONCURRENCY`, `RELAY_BAN_FAILURE_LIMIT`, `RELAY_BAN_FAILURE_WINDOW`, and
`RELAY_BAN_DURATION`. Headers are limited to 32 KiB, request bodies to 32 MiB, and tunnel frames
to 48 MiB. Operators may lower, but not raise, the protocol caps with
`RELAY_MAX_REQUEST_BODY_BYTES` and
`RELAY_MAX_TUNNEL_FRAME_BYTES`. Load-test the expected SSE count,
update size, and instance count, then provision file descriptors, memory, and cache storage.
A reverse proxy must allow WebSocket upgrades, disable SSE response buffering, and use an idle
timeout above Relay's 90 seconds. Only proxies in `RELAY_TRUSTED_PROXY_CIDRS` may supply source IPs.

See [Relay v1 protocol](relay-protocol.en.md) for frame, header-order, and retry boundaries.

## Security boundary

- Relay exposes only Desktop communication and read-only update routes; it is not a general HTTP/TCP proxy.
- Relay authenticates with a derived Argon2id verifier. Coworker's existing endpoint authentication verifies the original Bearer again.
- Five invalid Bearers for one instance and source IP within ten minutes trigger a one-hour ban.
- A missing Bearer does not count as a password failure, but anonymous traffic is rate-limited.
- Enrollment, tunnel authentication, credential rotation, and administrator APIs are also
  source-IP-rate-limited before credential verification.
- Relay appends `X-Coworker-Relay-*` while preserving client-supplied duplicates. Coworker identifies Relay traffic through the authenticated tunnel, not headers.
- Private Relay deployments must use a system-trusted private CA. TLS verification cannot be disabled.
- v1 provides no P2P, offline message storage, arbitrary upstream downloading, generic
  reverse proxy, or multi-node high availability.
