# Data and Trust Boundaries

[中文](data-boundaries.md) · English

[← Back to Architecture and Core Concepts](README.en.md)

Coworker is a locally operated autonomous agent, but “running locally” does not mean that data never leaves the device. It calls the model providers and tools you configure as tasks require. This page describes the default boundaries; individual model services and third-party integrations remain subject to their own privacy policies, logging practices, and deployment settings.

## Data stored locally by default

The following paths are relative to Coworker's working directory unless configuration overrides them:

| Path | Main contents |
|---|---|
| `.env`, `providers.json` | External configuration such as provider endpoints, models, and API keys |
| `data/admin_config.json` | Administrator token and settings saved through the administration page, which may include API keys |
| `data/` | Identity, memory stores, tasks, inboxes and outboxes, attachments, screenshots, runtime state, and logs |
| `.coworker/` | Skills, memory palaces, subconscious modes, and user changes to them |
| Desktop application data directory | Desktop settings, credentials, bridge state, and logs; the operating system determines the exact location |

These files may contain conversations, prompts, tool arguments and results, webpage content, file content, and personal information. `.env`, `providers.json`, and the administration configuration are ordinary local files; Coworker core does not encrypt them for you. Protect them with operating-system permissions, disk encryption, and a least-privileged account. Configuration export bundles include runtime data and secrets and must be handled as credential files.

The default configuration does not automatically synchronize the entire `data/` or `.coworker/` directory to a project-operated server, and Chroma anonymous telemetry is explicitly disabled. Downloads made by third-party dependencies, model services, and the tools below still produce network requests.

## Data that may leave the machine

- Model calls send the system prompt and the conversations, memories, tool results, and attachment contents needed for the current task. Coworker does not upload the whole working directory unconditionally, but file content read by the agent may later enter model context.
- `visual_analyze` sends selected images or videos to the configured vision model service.
- Search tools send queries. Browser tools visit target websites and are subject to those sites' logging, cookie, and session policies.
- WeCom, the Desktop bridge, and other communication or MCP integrations transmit messages, attachments, and protocol metadata to their corresponding services.
- Installing dependencies, Playwright browsers, or local embedding models connects to package registries, browser download servers, or model repositories.

If data must not be shared with an external service, do not configure that service for Coworker and do not let the agent read the relevant files. A self-hosted model changes only the model boundary; it does not automatically restrict search, browser, or other integrations.

## Execution and network boundaries

- Coworker is not a security sandbox. Command, file, and browser tools run with the permissions of the operating-system user that started the process. Use a dedicated least-privileged account, container, or virtual machine, and mount only disposable or backed-up directories.
- Treat webpages, messages, attachments, skills, memory, and model output as untrusted input. Any of them may contain prompt injection or malicious content.
- The API binds to `127.0.0.1` by default. The administrator token protects the administration API, but the current v0.x releases do not provide a complete multitenant authorization boundary for every route. Do not expose port 8000 directly. Remote deployments require TLS, trusted CORS origins, a strong communication token, and additional network access controls. See the [security policy](../../SECURITY.md).

## Inspection, backup, and cleanup

Stop Coworker first so files are not being written during cleanup. In a source checkout, inspect the scope of `data/` with:

```bash
uv run python scripts/cleanup.py status
```

When resetting runtime data, prefer backing it up before deletion:

```bash
uv run python scripts/cleanup.py backup-delete
```

`cleanup.py` handles only runtime files under `data/` and preserves `data/_backups/`, so `backup-delete` is not a secure erase. It also does not remove `.env`, `providers.json`, `.coworker/`, `credentials/`, Desktop application data, or Docker volumes. For complete removal, inspect and delete each of those locations and `data/_backups/` only after confirming recovery is no longer needed. Container deployments must also inspect bind-mounted directories and named volumes instead of deleting only the container.

[← Back to project home](../../README.en.md)
