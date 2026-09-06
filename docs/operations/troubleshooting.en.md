# Troubleshooting

[简体中文](troubleshooting.md) · English

[← Back to Configuration and Operations](README.en.md)

This page provides one diagnostic order for the Coworker service, management
console, models, Desktop, Relay, and container deployment: evidence is gathered
before state changes. Deleting `data/`, configuration, Docker volumes, or
reinstalling the application removes the state that diagnosis depends on.

## General diagnostic order

1. **Narrow the scope**: did the service fail to start, is the management page
   unavailable, did a model call fail, or is only one Channel or actor affected?
2. **Record time and version**: note when it happened, Coworker/Desktop versions,
   how it is running, and the most recent change.
3. **Inspect status**: use the terminal, Management Console Diagnostics and
   Audit, Desktop Status, or Relay health.
4. **Align logs**: the first ERROR or WARN in the same time range marks the
   origin; later entries are cascading results.
5. **Verify configuration source**: confirm the file, environment, and working
   directory the current process actually uses.
6. **Apply the smallest recovery**: retry one connection or perform a safe
   restart; restore, cleanup, and migration rewrite runtime data, so a backup
   usually precedes them.

A problem report can pick up Bearer tokens, API keys, Relay private keys,
QR-code content, complete message bodies, or unreviewed configuration exports.

## Coworker does not start

Check:

```bash
uv --version
python3 --version
uv run python scripts/check_version.py
```

- the Python version satisfies the project requirement;
- dependencies come from the current checkout's lock file;
- one working directory runs a single Coworker process at a time;
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

Memory and identity data are unrelated to a browser dependency failure.

## Management page unavailable or sign-in rejected

### Page is unreachable

- Confirm the Coworker process is still running.
- The default URL is <http://127.0.0.1:8000/admin>.
- For containers, check port mapping and container health.
- From another machine, publishing port `8000` drops the loopback-binding
  protection; controlled network access or the supported Relay scenario is the
  typical cross-device path.

### Token is rejected

- The effective administrator token is the one printed by the current startup
  terminal.
- Confirm the process reads `data/admin_config.json` from the expected working
  directory.
- Desktop communication, Relay, and administrator tokens are three distinct,
  non-interchangeable token types.
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

After a change of the long-term-memory embedding model, new writes use the new
model immediately; existing Chroma data cannot be assumed compatible with the
other embedding model, so migration requirements come first.

## Memory, task, or context problems

- Short context is too large: the common path is to inspect the message tail and
  memory tree first, then decide on full compression.
- Backfill keeps running: progress is visible through `GET /backfill_tree` or the
  console; running offline backfill at the same time repeats work over the same
  history.
- Long-term memory is not found: verify the mem0 Provider, embedding model, and
  database path did not change.
- Recent state is missing after restart: inspect the short-term snapshot,
  `data/logs`, and emergency backups in Runtime Center.
- A task or alarm does not fire: check time zone, Passive mode, Agent state, and
  whether it was canceled.

Emergency backup recovery has two levels:

- try summary restore first to re-inject history into the current context;
- use full restore only when the current short-term context must be replaced.

The common practice is to record the current version, backup filename, and
message count before restoring. Emergency short-term backups do not replace a
backup of the complete runtime directory.

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
plaintext. Replacing a Relay URL with a public Coworker port does not bypass the
failure; identity and encryption checks still reject the connection.

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
- An asset with a missing signature or a signature that does not match the
  embedded public key is rejected.
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
- Connection metadata visible in Relay logs does not imply an ability to decrypt
  message content.

See [Self-hosted Relay](relay.en.md) for deployment, pairing, blocking, backup,
and recovery. Protocol and certificate-identity failures do not downgrade
automatically; that is part of the security boundary.

## Docker and offline images

- Code, state, and model cache may use different volumes; resolve actual mounts
  first.
- The `offline` image blocks automatic downloads of missing Hugging Face content and prevents the
  startup initializer from cloning a workspace from a Git remote, but it is not a network sandbox.
  Model services and user-authorized Agent network tools may still connect.
- The preloaded embedding model must match runtime configuration.
- The dependency environment does not pick up changes to `pyproject.toml` or
  `uv.lock` automatically; a rebuild is required.
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
remove `.env`, `providers.json`, `.coworker/`, Desktop data, or Docker volumes;
the detailed impact scope is in
[Data and Trust Boundaries](../architecture/data-boundaries.en.md#inspection-backup-and-cleanup).

## Gather shareable diagnostic information

Typical diagnostic information includes:

- Coworker, Desktop, and Relay versions;
- operating system, CPU architecture, and run method;
- incident time and time zone;
- minimal reproduction;
- the first relevant error with a small amount of surrounding log context;
- whether only one actor, Channel, or instance is affected;
- the most recent successful operation;
- attempted recovery actions and results.

The following items carry credentials or personal information:

- Authorization headers, tokens, API keys, and private keys;
- complete configuration exports;
- user messages, attachments, and file contents;
- Relay pairing material and WeChat QR codes;
- unrelated personal paths and identity information.

Security vulnerabilities and possible credential exposure follow the private
reporting channel defined by the [Security Policy](../../SECURITY.en.md); a
public issue is not the channel for them.

[← Back to the project home](../../README.en.md)
