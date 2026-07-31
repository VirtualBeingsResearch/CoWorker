# Upgrading and Migration

[中文](upgrading.md) · English

[← Back to Configuration and Operations](README.en.md)

This page is for instances that already contain identity, memory, tasks, or client connections.
Do not treat an upgrade as a code-only operation. Record the current version, create a backup,
and check for data-format or protocol changes first.

## Before upgrading

1. Read the [CHANGELOG](../../CHANGELOG.md), especially migration, compatibility, and known-limit sections.
2. Confirm that no task is failing continuously and that no memory backfill or critical tool call is running.
3. Record the Coworker, Desktop, and Relay versions and how each component is deployed.
4. Back up `data/`, `.coworker/`, configuration, and external components using
   [Backup and Restore](backup-and-restore.en.md).
5. Keep the currently working commit or image tag. Do not rely on a floating tag as the only rollback point.

> [!WARNING]
> Do not force-sync, reset, or overwrite a working tree with unreviewed changes. Coworker may also
> have created branches or commits in the checkout.

## Let Coworker upgrade herself

For a source checkout, the recommended path is to ask Coworker to inspect and perform the upgrade.
She can use file, code, and command tools to inspect the working tree and remotes, review upstream
changes, resolve conflicts whose intent is clear, run relevant checks, and call `restart_self`
separately after the code update is ready.

For example:

> Inspect the current repository and runtime, back up data affected by the upgrade, then safely
> integrate upstream changes into the current branch. Preserve local commits and modifications,
> review migrations and dependency changes, and run the relevant checks. Ask me before discarding
> or overwriting work or resolving an ambiguous conflict. When everything passes, call
> `restart_self` by itself, then report the restored version and validation results.

`restart_self` protects the transition as follows:

1. runs `python -m coworker --check` in the current Python environment, with a 30-second timeout;
2. returns an error and leaves the current process running when validation fails or times out;
3. saves short-term memory with the pending tool call after validation succeeds;
4. requests the main loop to exit, then lets the launcher replace the process in place; on Windows,
   the parent supervisor starts a new worker;
5. restores short-term memory and alarms, replacing the pending call with a real restart result.

The main line must call the tool by itself; a Bubble cannot trigger it. The check proves only that
the new code can load configuration and register Providers. It does not prove every integration
test, data migration, or external service works, so the upgrade task must still back up data,
review the diff, and run relevant tests first.

If Coworker is wrapped by a process manager that does not support process replacement, or the
upgrade changes the container image, system dependencies, launch command, or Python environment,
the host or orchestrator must perform that outer upgrade. `restart_self` can restart only the
current runtime environment.

## Manually upgrade a source checkout

Stop Coworker, then inspect the current branch and remotes:

```bash
git status --short
git branch --show-current
git remote -v
```

Fetch and review the intended upstream through your normal Git workflow. When the lock file changes:

```bash
uv sync --frozen
uv run playwright install chromium
uv run coworker --check
```

`--check` validates the startup environment without entering the persistent Agent loop. After it
passes, start with `uv run coworker`, then inspect `/status` and management diagnostics.

## Upgrade Docker Compose

After stopping writes and creating a backup, select the exact image or source revision to run:

```bash
docker compose stop
docker compose build
docker compose up -d
docker compose ps
```

For published images, pin `COWORKER_IMAGE` to the tag or digest you intend to validate, then run
`docker compose pull` and `docker compose up -d`. `coworker-workspace`, `coworker-state`, and
`coworker-models` are separate volumes. Replacing a container does not migrate, back up, or delete them.

## Data and memory migration

- Do not assume an older release can read data written by a newer release.
- After a memory-tree upgrade, new compression events extend the tree by default. To backfill
  historical logs, use the management console or `POST /backfill_tree`. Backfill makes model calls.
- Changing the embedding model changes the vector space of long-term memory. Do not switch an
  existing store unless release notes provide an explicit rebuild path.
- Identity, Skills, Palaces, subconscious modes, tasks, and history are not translated when the
  runtime locale changes.

See [Core Concepts and Capabilities](../architecture/concepts.en.md) for memory internals and
notes about specific legacy formats.

## Component compatibility

- Go Relay, Python Coworker, and Rust Desktop use Relay protocol version `1`. Upgrade them together
  when protocol or key derivation changes; see [Relay v1 Protocol](relay-protocol.en.md).
- Desktop negotiates a protocol version with Coworker. An old client may continue to work locally
  while being unable to connect to an incompatible Coworker or Relay.
- A failed Desktop update does not remove the installed version. Missing signatures or platform
  assets must leave the old version in place.

## Validate after upgrading

- `/status` reports the expected runtime, Provider, and model;
- management diagnostics has no continuously growing failures;
- a test message completes through the intended reply path;
- long-term memory, tasks, alarms, and recent interactions remain readable;
- local Desktop and remote Relay connections work when applicable;
- the upgrade time, target version, backup, and results are recorded.

## Rollback

Code rollback and data restore are separate operations. Switch only the code or image when the old
release is known to support the current data. If the new release wrote an incompatible format,
stop the service, preserve the failed state, and restore the complete pre-upgrade backup. Do not
experiment by deleting `data/` or Docker volumes.

[← Back to project home](../../README.en.md)
