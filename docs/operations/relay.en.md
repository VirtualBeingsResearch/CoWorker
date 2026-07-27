# Self-hosted Relay

Coworker Relay lets a Coworker inside a private network open an encrypted outbound connection and expose Desktop status, registration, messaging, SSE, and update endpoints through one path:

```text
https://relay.example.com/i/{instance_id}
```

Relay terminates public HTTPS and can see headers and bodies while processing a request. It is not end-to-end encryption, but it does not persist message bodies, attachments, or SSE events.

## Deployment

The Go service and management CLI live in `apps/coworker-relay/`:

```bash
cd apps/coworker-relay
docker build -t coworker-relay .
go build -o relayctl ./cmd/relayctl
./relayctl init --public-url https://relay.example.com:8443
cd coworker-relay-deploy
docker compose up -d
```

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

Enter the one-time code on Coworker's Remote access page. It expires after ten minutes and works once. After pairing, copy the displayed Base URL and Bearer Token into Desktop.

## Administration

```bash
relayctl instance list
relayctl instance update-auth cw_xxx optional
relayctl instance update-auth cw_xxx required
relayctl bans list --instance cw_xxx
relayctl bans remove --instance cw_xxx --ip 203.0.113.8 --reason "false positive"
relayctl instance revoke cw_xxx
```

Desktop updates start in `optional` mode so older builds can update anonymously. New builds attach the Coworker Bearer to update checks and package downloads. Switch to `required` after older clients have migrated.

## Security boundary

- Relay exposes only Desktop communication and read-only update routes; it is not a general HTTP/TCP proxy.
- Relay authenticates with a derived Argon2id verifier. Coworker's existing endpoint authentication verifies the original Bearer again.
- Five invalid Bearers for one instance and source IP within ten minutes trigger a one-hour ban.
- A missing Bearer does not count as a password failure, but anonymous traffic is rate-limited.
- Relay appends `X-Coworker-Relay-*` while preserving client-supplied duplicates. Coworker identifies Relay traffic through the authenticated tunnel, not headers.
- Private Relay deployments must use a system-trusted private CA. TLS verification cannot be disabled.
