# Data and Trust Boundaries

[中文](data-boundaries.md) · English

[← Back to Architecture and Core Concepts](README.en.md)

Coworker is a locally operated autonomous agent, but “running locally” does not mean that data never leaves the device. It calls the model providers and tools you configure as tasks require. This page describes the default boundaries; individual model services and third-party integrations remain subject to their own privacy policies, logging practices, and deployment settings.

## Data stored locally by default

The following paths are relative to Coworker's working directory unless configuration overrides them:

| Path | Main contents |
|---|---|
| `.env`, `providers.json` | External configuration such as provider endpoints, models, and API keys |
| `data/admin_config.json` | Administrator token and settings saved through the administration page, which may include API keys and a custom system prompt template |
| `data/` | Identity, memory stores, tasks, inboxes and outboxes, attachments, screenshots, runtime state, and logs |
| `.coworker/` | Skills, memory palaces, subconscious modes, and user changes to them |
| Desktop application data directory | Desktop settings, credentials, bridge state, and logs; the operating system determines the exact location |
| `relay` fields in Coworker administration configuration | Self-hosted Relay URL, instance ID, instance private key, and pinned Relay public key |
| Relay's separate data volume | Instance public keys, authentication epochs, pairing state, source-IP bans/failures, audit events, and aggregate traffic statistics |

These files may contain conversations, prompts, tool arguments and results, webpage content, file content, and personal information. `.env`, `providers.json`, and the administration configuration are ordinary local files; Coworker core does not encrypt them for you, and their protection comes from operating-system permissions, disk encryption, and a least-privileged account. Configuration export bundles include runtime data and secrets and are credential-equivalent files.

The default configuration does not automatically synchronize the entire `data/` or `.coworker/`
directory to a project-operated server, and explicitly disables anonymous telemetry from mem0 and
Chroma. Setting `MEM0_TELEMETRY=true` or `ANONYMIZED_TELEMETRY=true` explicitly in the process
environment opts back into the corresponding telemetry. Downloads made by third-party dependencies,
model services, and the tools below still produce network requests.

By default, Compose bind-mounts the host Git checkout as the workspace and keeps runtime data and
the model cache in the separate `coworker-state` and `coworker-models` volumes. When running the
image directly or setting `COWORKER_WORKSPACE_SOURCE=coworker-workspace`, the `offline` image
initializes a workspace volume from its embedded Git bundle without having the startup initializer
contact a repository remote, and it blocks automatic runtime downloads of missing Hugging Face
content. This is not container-level network isolation: model Providers and user-authorized Agent
tasks that use Git, search, a browser, or integrations may still access the network. Deleting a
volume or the host checkout also deletes the corresponding history, state, or model cache.

## Data that may leave the machine

- Model calls send the system prompt and the conversations, memories, tool results, and attachment contents needed for the current task. Coworker does not upload the whole working directory unconditionally, but file content read by the agent may later enter model context.
- Custom system prompt text is administrator-trusted input. It enters the system prompt verbatim
  and is sent to the model provider; anything pasted into the template, including secrets, leaves
  the machine the same way.
- `visual_analyze` sends selected images or videos to the configured vision model service.
- Search tools send queries. Browser tools visit target websites and are subject to those sites' logging, cookie, and session policies.
- WeCom, Telegram, the Desktop bridge, and other communication or MCP integrations transmit messages, attachments, and protocol metadata to their corresponding services.
- With self-hosted Relay enabled, Relay can observe source IP, instance, connection times, sizes,
  and timing. Bearers, headers, paths, message bodies, attachments, SSE events, and update artifacts
  remain inside inner TLS 1.3 between Desktop and Coworker, so Relay cannot decrypt them or create a
  valid client request. Relay can still interrupt, delay, or rate-limit connections.
- Installing dependencies, Playwright browsers, or local embedding models connects to package registries, browser download servers, or model repositories.

If data must not reach an external service, configuring that service for Coworker or letting the
agent read the relevant files is what sends the data across that boundary. A self-hosted model
changes only the model boundary; it does not automatically restrict search, browser, or other integrations.

## Execution and network boundaries

- Coworker is not a security sandbox. Command, file, and browser tools run with the permissions of the operating-system user that started the process; the reach of those tools is determined by that account's permissions and the mounted directories.
- Webpages, messages, attachments, skills, memory, and model output are untrusted input. Any of them may contain prompt injection or malicious content.
- The API binds to `127.0.0.1` by default. The administrator token protects the administration API, but the current v0.x releases do not provide a complete multitenant authorization boundary for every route; exposing port 8000 directly bypasses the loopback-binding protection. Remote deployments require TLS, trusted CORS origins, a strong communication token, and additional network access controls. See the [security policy](../../SECURITY.en.md).
- Desktop's public access path is [self-hosted Relay](../operations/relay.en.md) rather than a
  published Coworker port 8000. Relay v1 is a single-node public security boundary, not a
  multitenant isolation platform or general proxy.

## Inspection, backup, and cleanup

Stop Coworker first so files are not being written during cleanup. In a source checkout, inspect the scope of `data/` with:

```bash
uv run python scripts/cleanup.py status
```

Resetting runtime data backs it up before deletion:

```bash
uv run python scripts/cleanup.py backup-delete
```

`cleanup.py` handles only runtime files under `data/` and preserves `data/_backups/`, so `backup-delete` is not a secure erase. It also does not remove `.env`, `providers.json`, `.coworker/`, `credentials/`, Desktop application data, or Docker volumes. Complete removal therefore means handling each of those locations and `data/_backups/` individually, after confirming recovery is no longer needed. Deleting a container leaves its bind-mounted directories and named volumes in place.

Relay data is outside `cleanup.py`'s scope. Run `coworker-relay backup` before deletion. Use
`coworker-relay instance revoke` for one instance; it cascades through its public key,
authentication epoch, pairing state, and failure/ban state. Full decommissioning also requires
removing `.env`, the signing key, backups, and the Relay deployment's separate Docker volume.

[← Back to project home](../../README.en.md)
