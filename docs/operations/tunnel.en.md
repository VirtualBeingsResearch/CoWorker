# Public access (Cloudflare Tunnel)

English · [中文](tunnel.md)

[← Back to configuration and operations](README.en.md)

Cloudflare Tunnel lets `cloudflared` open an outbound connection from this machine and map
`http://127.0.0.1:<API__PORT>` to an HTTPS hostname. You do not open a router port or need a public
IP. Use it for **your own** access to the control room from a phone or another computer, or for
external clients that call `/v1`.

It does not replace the [self-hosted Relay](relay.en.md). Relay is the end-to-end encrypted Desktop
path; Tunnel terminates TLS at Cloudflare's edge, so **Cloudflare can read plaintext**. Use Relay
when confidentiality matters.

## Two modes

| | named (configured tunnel) | quick (temporary tunnel) |
|---|---|---|
| Hostname | Fixed across restarts | Random `*.trycloudflare.com` per start |
| Cloudflare account | Required | Not required |
| Web chat `/sse` | Works | **Does not work** (quick tunnels buffer SSE) |
| Runtime log `/logs/stream` | Works | **Does not work** |
| Desktop direct connection | Works | Does not work |
| Admin console polling | Works | Works |
| Best for | Long-term use | Trying it out, troubleshooting |

## Prerequisites

1. **Set `API__COMMUNICATION_TOKEN` explicitly.** On a default instance `/messages`, `/sse`, `/ws`,
   and `/logs/stream` require no authentication; opening a tunnel without a token publishes them.
2. **Finish initial setup.** The tunnel cannot be enabled while setup is incomplete.
3. Install `cloudflared`. The official container image already includes it; see below for source and
   system installs.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TUNNEL__MODE` | `off` | `off`, `quick`, or `named` |
| `TUNNEL__TOKEN` | empty | Required for named; tunnel token from Cloudflare Zero Trust |
| `TUNNEL__PUBLIC_URL` | empty | Required for named; public origin such as `https://coworker.example.com` |
| `TUNNEL__PROTOCOL` | `http2` | `http2`, `quic`, or `auto`; keep `http2` on restricted networks |
| `TUNNEL__BINARY` | empty | Optional `cloudflared` path; defaults to a `PATH` lookup |
| `TUNNEL__QUICK_ALLOWED_EMAILS` | `[]` | Quick-mode email allowlist handled by cloudflared |

`TUNNEL__TOKEN` is a protected setting: the console shows only whether it is configured and its last
four characters, never the full value.

## Enable a named tunnel

1. In Cloudflare Zero Trust, create a Tunnel and add a Public Hostname pointing at
   `http://127.0.0.1:8000` (`http://0.0.0.0:8000` does not apply; the API listens inside the
   container on port 8000).
2. Copy the tunnel token into the console under "Remote access → Public access", and enter the same
   public hostname.
3. Select "Connect". When the state becomes connected, scan the QR code to open the console on a
   phone.

The tunnel probes its public URL every 60 seconds; two consecutive failures restart `cloudflared`.
If the host crashes, the next start reaps the leftover child by exact pid, creation time, and command
line.

## Enable a quick tunnel

Choose "Temporary" in the same card and connect. The hostname changes on every start and old links
stop working immediately. It is only for temporary checks: web chat, runtime logs, and Desktop do not
work, and Cloudflare provides no availability guarantee.

## Install cloudflared (source installs)

The official container image already includes a pinned `cloudflared`. For source or system installs,
install it before enabling the tunnel:

```bash
# Windows
winget install --id Cloudflare.cloudflared

# macOS
brew install cloudflared

# Debian / Ubuntu: add Cloudflare's package repository, then
apt install cloudflared

# Other platforms
# https://github.com/cloudflare/cloudflared/releases
```

The application never downloads or upgrades `cloudflared` at runtime. When the binary is missing,
the console and the API error include the platform-specific install hint.

## Desktop connection

Desktop can connect through a named tunnel: production mode requires `https://` plus a Bearer token,
which the tunnel provides, so the client needs no changes. Two limits apply:

- Messages, attachments, and SSE content are plaintext at Cloudflare's edge; use Relay when you need
  end-to-end encryption.
- Desktop cannot send Cloudflare Access service-token headers, so **do not enable Access on the
  hostname Desktop uses**. If you need Access, split the console and Desktop onto two hostnames.

## Capability matrix

| Capability | named | quick |
|---|---|---|
| Admin console and polling | Works | Works |
| Web chat `/sse` | Works | Does not work |
| Runtime log `/logs/stream` | Works | Does not work |
| Chat WebSocket (scripts, external clients) | Works | Unverified |
| Desktop direct connection | Works | Does not work |
| `/v1` short requests (under 100 s) | Works | Works |
| `/v1` streaming | Works | Does not work |
| Request bodies under 100 MB | Works | Works |
| Desktop release upload | Does not work (413) | Does not work (413) |
| Memory compression, backfill, backup restore | Not recommended (524) | Not recommended |
| Initial setup | Does not work | Does not work |

## Troubleshooting

| Symptom | Usual cause | Action |
|---|---|---|
| Cloudflare 1033 or local 502 | `cloudflared` cannot reach the edge | Keep `TUNNEL__PROTOCOL=http2`; check outbound network |
| Cloudflare 530 | The hostname lost its edge registration | The watchdog restarts it; if it persists, check the token and hostname |
| 524 | No response headers within 100 s | Use streaming `/v1`; run long jobs locally |
| 413 | Request body over 100 MB | Publish artifacts and restore locally |
| Chat or logs connect but receive nothing | A quick tunnel is in use | Switch to named |
| 401 / 403 | Missing or wrong token, or an Access policy | Check the Bearer token and the Access policy |

## Security boundary

- Cloudflare's edge can read plaintext, unlike Relay's end-to-end encryption.
- Enable **Cloudflare Access** (SSO or email PIN) on the console hostname; browser cookies work
  without application changes.
- Rate-limit `/api/admin/session/verify` and `/api/admin/*` at Cloudflare; the application does not
  rate-limit them.
- The audit log records connect, disconnect, reconnect, and test actions, plus the real client IP
  from `X-Forwarded-For`.
- Do not change `API__HOST` to `0.0.0.0` for the tunnel: `cloudflared` connects over loopback.

## Verification checklist

- [ ] `API__COMMUNICATION_TOKEN` is set explicitly and differs from the administrator token.
- [ ] A named tunnel keeps its hostname across restarts, and both the console and chat work.
- [ ] `/v1/chat/completions` with `stream=true` keeps delivering events.
- [ ] After killing the Coworker process, no `cloudflared` child is left behind.
- [ ] After disconnecting the network, the watchdog brings the tunnel back.
