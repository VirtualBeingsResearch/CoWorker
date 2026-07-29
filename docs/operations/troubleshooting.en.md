# Troubleshooting

[简体中文](troubleshooting.md) · English

[← Back to Configuration and Operations](README.en.md)

This page provides one diagnostic order for the Coworker service, management
console, models, Desktop, Relay, and container deployment. Gather evidence
before changing state. Do not make deletion of `data/`, configuration, Docker
volumes, or application reinstallation the first step.

## General diagnostic order

1. **Narrow the scope**: did the service fail to start, is the management page
   unavailable, did a model call fail, or is only one Channel or actor affected?
2. **Record time and version**: note when it happened, Coworker/Desktop versions,
   how it is running, and the most recent change.
3. **Inspect status**: use the terminal, Management Console Diagnostics and
   Audit, Desktop Status, or Relay health.
4. **Align logs**: find the first ERROR or WARN in the same time range rather
   than only the last cascading error.
5. **Verify configuration source**: confirm the file, environment, and working
   directory the current process actually uses.
6. **Apply the smallest recovery**: retry one connection or perform a safe
   restart; back up before restore, cleanup, or migration.

Do not include Bearer tokens, API keys, Relay private keys, QR-code content,
complete message bodies, or unreviewed configuration exports in a problem
report.

## Coworker does not start

Check:

```bash
uv --version
python3 --version
uv run python scripts/check_version.py
```

- Python satisfies the project requirement;
- dependencies were installed from the current checkout's lock file;
- only one Coworker process uses a working directory;
- `data/` is writable and the disk is not full;
- no other process occupies port `8000`;
- Intel macOS runs the Python service through the Dev Container or Docker.

If only browser tools fail while the Agent starts, install Chromium:

```bash
uv run playwright install chromium
```

On Debian or Ubuntu with missing system libraries:

```bash
uv run playwright install --with-deps chromium
```

Do not clear memory or identity data because a browser dependency failed.

## Management page unavailable or sign-in rejected

### Page is unreachable

- Confirm the Coworker process is still running.
- The default URL is <http://127.0.0.1:8000/admin>.
- For containers, check port mapping and container health.
- From another machine, do not temporarily publish port `8000`; use controlled
  network access or the supported Relay scenario.

### Token is rejected

- Use the effective administrator token printed by the current startup terminal.
- Confirm the process reads `data/admin_config.json` from the expected working
  directory.
- Do not confuse Desktop communication, Relay, and administrator tokens.
- If the browser retained an old token, sign out of the management session and
  enter the current one.

Before first-time setup completes, ordinary pages and APIs redirect to `/admin`,
and the Agent loop and external Channels do not start. This is setup mode, not
a runtime failure.

## Model or Provider calls fail

Check in order:

1. current Provider, model ID, and fallback in Model Orchestration;
2. saved Provider API key, Base URL, and TLS settings in Runtime Settings;
3. whether the model and gateway support tool/function calling;
4. account quota, rate limit, and network access;
5. whether summary, vision, and main models point to different or retired
   Providers unexpectedly;
6. the first upstream response status in logs rather than later recovery errors.

A manually entered model is not probed online. Successful plain-text generation
does not prove tool-calling support.

If you just changed the long-term-memory embedding model, stop further writes
and inspect migration requirements. Existing Chroma data cannot be assumed
compatible with a different embedding model.

## Memory, task, or context problems

- Short context is too large: inspect the message tail and memory tree before
  triggering full compression.
- Backfill keeps running: inspect `GET /backfill_tree` or the console progress;
  do not run offline backfill at the same time.
- Long-term memory is not found: verify the mem0 Provider, embedding model, and
  database path did not change.
- Recent state is missing after restart: inspect the short-term snapshot,
  `data/logs`, and emergency backups in Runtime Center.
- A task or alarm does not fire: check time zone, Passive mode, Agent state, and
  whether it was canceled.

Emergency backup recovery has two levels:

- try summary restore first to re-inject history into the current context;
- use full restore only when the current short-term context must be replaced.

Record current version, backup filename, and message count before restoring.
Emergency short-term backups do not replace a backup of the complete runtime
directory.

## Desktop cannot connect

Save all Desktop changes, select the target Coworker, and run diagnostics in
Status.

### Coworker diagnostic fails

- Confirm the URL belongs to the selected instance.
- A direct production address must use HTTPS.
- The Bearer token must be the administrator token or dedicated Desktop
  communication token.
- Coworker must have completed first-time setup.
- Its Desktop Channel runtime may still be starting or restarting.
- With Relay, the URL must contain the exact correct instance path.

Identity, protocol, or end-to-end-encryption failure does not downgrade to
plaintext. Do not replace a Relay URL with a public Coworker port to bypass the
error.

### Codex or Claude is unavailable

- Confirm the CLI is installed and works in a normal terminal.
- Confirm sign-in is complete.
- Enter an absolute command path only when automatic discovery fails.
- Save the command change and rerun diagnostics.
- One failed actor does not prevent Local or another healthy actor from working.

### A conversation cannot continue

- A lock marks read-only history.
- A native App/CLI conversation must be verified by its app-server before
  resuming.
- A deleted or expired native conversation ID is rejected before writing.
- During an active actor turn you can append input, but cannot switch mode in
  the same message.
- A stopped Bridge cannot create or write conversations.

### A message did not reach Coworker

An ordinary Codex or Claude `final` remains local. Use **Send to Coworker** or
`send_to_coworker` explicitly. Confirm:

- the correct Coworker was selected;
- the Bridge is running;
- the target remains a known participant;
- attachment paths remain readable;
- logs contain no outbox or ACK failure.

### Update fails

- Confirm the update URL matches the current Coworker or Relay instance.
- Check client version and target architecture.
- A missing signature or signature that does not match the embedded public key
  must be rejected.
- The temporary Relay update adapter permits only fixed paths for the current
  instance, not arbitrary URLs or cross-instance redirects.

An update failure does not prevent continued use of the installed version.
Preserve logs before contacting a release maintainer.

## Relay connection fails

First locate the failing side:

`Desktop → Relay`, `Coworker → Relay`, or Relay itself.

- Run the connection test in Management Console Remote Access.
- Check Relay health, DNS, certificates, system time, and instance state.
- Check whether a token was rotated or an instance revoked.
- Check whether repeated failures blocked the source IP.
- Do not confuse connection metadata visible in Relay logs with an ability to
  decrypt messages.

See [Self-hosted Relay](relay.en.md) for deployment, pairing, blocking, backup,
and recovery. Protocol and certificate-identity failures do not downgrade
automatically; that is part of the security boundary.

## Docker and offline images

- Code, state, and model cache may use different volumes; resolve actual mounts
  first.
- A strict offline image does not fetch missing content from Hugging Face or Git
  remotes at runtime.
- The preloaded embedding model must match runtime configuration.
- Rebuild the dependency environment after changing `pyproject.toml` or
  `uv.lock`.
- In mounted-checkout mode, the host and Agent see the same Git workspace.

Inspect the data scope:

```bash
uv run python scripts/cleanup.py status
```

To back up and then remove runtime data:

```bash
uv run python scripts/cleanup.py backup-delete
```

`backup-delete` covers only `data/`, preserves `data/_backups/`, and does not
remove `.env`, `providers.json`, `.coworker/`, Desktop data, or Docker volumes.
Read [Data and Trust Boundaries](../architecture/data-boundaries.en.md#inspection-backup-and-cleanup)
before running it.

## Gather shareable diagnostic information

Include:

- Coworker, Desktop, and Relay versions;
- operating system, CPU architecture, and run method;
- incident time and time zone;
- minimal reproduction;
- the first relevant error with a small amount of surrounding log context;
- whether only one actor, Channel, or instance is affected;
- the most recent successful operation;
- attempted recovery actions and results.

Remove:

- Authorization headers, tokens, API keys, and private keys;
- complete configuration exports;
- user messages, attachments, and file contents;
- Relay pairing material and WeChat QR codes;
- unrelated personal paths and identity information.

Report security vulnerabilities or possible credential exposure privately
through the [Security Policy](../../SECURITY.en.md), not a public issue.

[← Back to the project home](../../README.en.md)
